#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

import datetime
import hashlib
import os
import pathlib
import re
import shutil
import sys
import tempfile
import urllib
import uuid

import lxml.html
import youtube_dl
from bs4 import BeautifulSoup
from kiwixstorage import KiwixStorage
from pif import get_public_ip
from slugify import slugify
from zimscraperlib.download import save_large_file
from zimscraperlib.imaging import resize_image, convert_image
from zimscraperlib.video.encoding import reencode
from zimscraperlib.video.presets import VideoMp4Low, VideoWebmLow
from zimscraperlib.zim import make_zim_file

from .annex import MoocForum, MoocWiki
from .constants import (
    DOWNLOADABLE_EXTENSIONS,
    IMAGE_FORMATS,
    OPTIMIZER_VERSIONS,
    ROOT_DIR,
    SCRAPER,
    VIDEO_FORMATS,
    getLogger,
)
from .instance_connection import InstanceConnection
from .utils import (
    check_missing_binary,
    exec_cmd,
    get_meta_from_url,
    jinja,
    jinja_init,
    prepare_url,
)
from .xblocks_extractor.chapter import Chapter
from .xblocks_extractor.course import Course
from .xblocks_extractor.discussion import Discussion
from .xblocks_extractor.drag_and_drop_v2 import DragAndDropV2
from .xblocks_extractor.free_text_response import FreeTextResponse
from .xblocks_extractor.html import Html
from .xblocks_extractor.libcast import Libcast
from .xblocks_extractor.lti import Lti
from .xblocks_extractor.problem import Problem
from .xblocks_extractor.sequential import Sequential
from .xblocks_extractor.unavailable import Unavailable
from .xblocks_extractor.vertical import Vertical
from .xblocks_extractor.video import Video

XBLOCK_EXTRACTORS = {
    "course": Course,
    "chapter": Chapter,
    "sequential": Sequential,
    "vertical": Vertical,
    "video": Video,
    "libcast_xblock": Libcast,
    "html": Html,
    "problem": Problem,
    "discussion": Discussion,
    "qualtricssurvey": Html,
    "freetextresponse": FreeTextResponse,
    "drag-and-drop-v2": DragAndDropV2,
    "lti": Lti,
    "unavailable": Unavailable,
}

logger = getLogger()


class Openedx2Zim:
    def __init__(
        self,
        course_url,
        email,
        password,
        video_format,
        low_quality,
        autoplay,
        name,
        title,
        description,
        creator,
        publisher,
        tags,
        ignore_missing_xblocks,
        lang,
        add_wiki,
        add_forum,
        s3_url_with_credentials,
        use_any_optimized_version,
        output_dir,
        tmp_dir,
        fname,
        no_fulltext_index,
        no_zim,
        keep_build_dir,
        debug,
    ):

        # video-encoding info
        self.video_format = video_format
        self.low_quality = low_quality

        # zim params
        self.fname = fname
        self.tags = [] if tags is None else [t.strip() for t in tags.split(",")]
        self.title = title
        self.description = description
        self.creator = creator
        self.publisher = publisher
        self.name = name
        self.lang = lang or "en"
        self.no_fulltext_index = no_fulltext_index

        # directory setup
        self.output_dir = pathlib.Path(output_dir).expanduser().resolve()
        if tmp_dir:
            pathlib.Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        self.build_dir = pathlib.Path(tempfile.mkdtemp(dir=tmp_dir))

        # scraper options
        self.course_url = course_url
        self.add_wiki = add_wiki
        self.add_forum = add_forum
        self.ignore_missing_xblocks = ignore_missing_xblocks
        self.autoplay = autoplay

        # authentication
        self.email = email
        self.password = password

        # optimization cache
        self.s3_url_with_credentials = s3_url_with_credentials
        self.use_any_optimized_version = use_any_optimized_version
        self.s3_storage = None

        # debug/developer options
        self.no_zim = no_zim
        self.debug = debug
        self.keep_build_dir = keep_build_dir

        # course info
        self.course_id = None
        self.instance_url = None
        self.course_info = None
        self.course_name_slug = None
        self.has_homepage = True

        # scraper data
        self.instance_connection = None
        self.xblock_extractor_objects = []
        self.head_course_xblock = None
        self.homepage_html = []
        self.annexed_pages = []
        self.book_lists = []
        self.course_tabs = {}
        self.course_xblocks = None
        self.root_xblock_id = None

    def get_course_id(self, url, course_page_name, course_prefix, instance_url):
        clean_url = re.match(
            instance_url + course_prefix + ".*" + course_page_name, url
        )
        clean_id = clean_url.group(0)[
            len(instance_url + course_prefix) : -len(course_page_name)
        ]
        if "%3" in clean_id:  # course_id seems already encode
            return clean_id
        return urllib.parse.quote_plus(clean_id)

    def prepare_mooc_data(self):
        self.instance_url = self.instance_connection.instance_config["instance_url"]
        self.course_id = self.get_course_id(
            self.course_url,
            self.instance_connection.instance_config["course_page_name"],
            self.instance_connection.instance_config["course_prefix"],
            self.instance_url,
        )
        logger.info("Getting course info ...")
        self.course_info = self.instance_connection.get_api_json(
            "/api/courses/v1/courses/"
            + self.course_id
            + "?username="
            + self.instance_connection.user
        )
        self.course_name_slug = slugify(self.course_info["name"])
        logger.info("Getting course xblocks ...")
        xblocks_data = self.instance_connection.get_api_json(
            "/api/courses/v1/blocks/?course_id="
            + self.course_id
            + "&username="
            + self.instance_connection.user
            + "&depth=all&requested_fields=graded,format,student_view_multi_device&student_view_data=video,discussion&block_counts=video,discussion,problem&nav_depth=3"
        )
        self.course_xblocks = xblocks_data["blocks"]
        self.root_xblock_id = xblocks_data["root"]

    def parse_course_xblocks(self):
        def make_objects(current_path, current_id, root_url):
            current_xblock = self.course_xblocks[current_id]
            xblock_path = current_path.joinpath(slugify(current_xblock["display_name"]))

            # update root url respective to the current xblock
            root_url = root_url + "../"
            random_id = str(uuid.uuid4())
            descendants = None

            # recursively make objects for all descendents
            if "descendants" in current_xblock:
                descendants = []
                for next_xblock_id in current_xblock["descendants"]:
                    descendants.append(
                        make_objects(xblock_path, next_xblock_id, root_url)
                    )

            # create objects of respective xblock_extractor if available
            if current_xblock["type"] in XBLOCK_EXTRACTORS:
                obj = XBLOCK_EXTRACTORS[current_xblock["type"]](
                    xblock_json=current_xblock,
                    relative_path=xblock_path,
                    root_url=root_url,
                    xblock_id=random_id,
                    descendants=descendants,
                    scraper=self,
                )
            else:
                if not self.ignore_missing_xblocks:
                    logger.error(
                        f"Unsupported xblock: {current_xblock['type']} URL: {current_xblock['student_view_url']}"
                        f"  You can open an issue at https://github.com/openzim/openedx/issues with this log and MOOC URL"
                        f"  You can ignore this message by passing --ignore-missing-xblocks in atguments"
                    )
                    sys.exit(1)
                else:
                    logger.warning(
                        f"Ignoring unsupported xblock: {current_xblock['type']} URL: {current_xblock['student_view_url']}"
                    )
                    # make an object of unavailable type
                    obj = XBLOCK_EXTRACTORS["unavailable"](
                        xblock_json=current_xblock,
                        relative_path=xblock_path,
                        root_url=root_url,
                        xblock_id=random_id,
                        descendants=descendants,
                        scraper=self,
                    )

            if current_xblock["type"] == "course":
                self.head_course_xblock = obj
            self.xblock_extractor_objects.append(obj)
            return obj

        logger.info("Parsing xblocks and preparing extractor objects")
        make_objects(
            current_path=pathlib.Path("course"),
            current_id=self.root_xblock_id,
            root_url="../",
        )

    def get_book_list(self, book, output_path):
        pdf = book.find_all("a")
        book_list = []
        for url in pdf:
            file_name = pathlib.Path(urllib.parse.urlparse(url["rel"][0]).path).name
            self.download_file(
                prepare_url(url["rel"][0], self.instance_url),
                output_path.joinpath(file_name),
            )
            book_list.append({"url": file_name, "name": url.get_text()})
        return book_list

    def annex_extra_page(self, tab_href, tab_org_path):
        output_path = self.build_dir.joinpath(tab_org_path)
        output_path.mkdir(parents=True, exist_ok=True)
        page_content = self.instance_connection.get_page(self.instance_url + tab_href)
        if not page_content:
            logger.error(f"Failed to get page content for tab {tab_org_path}")
            raise SystemExit(1)
        soup_page = BeautifulSoup(page_content, "lxml")
        just_content = soup_page.find("section", attrs={"class": "container"})

        # its a content page
        if just_content is not None:
            self.annexed_pages.append(
                {
                    "output_path": output_path,
                    "content": str(just_content),
                    "title": soup_page.find("title").get_text(),
                }
            )
            return f"{tab_org_path}/index.html"

        # page contains a book_list
        book = soup_page.find("section", attrs={"class": "book-sidebar"})
        if book is not None:
            self.book_lists.append(
                {
                    "output_path": output_path,
                    "book_list": book,
                    "dir_path": tab_org_path,
                }
            )
            return f"{tab_org_path}/index.html"

        # page is not supported
        logger.warning(
            "Oh it's seems we does not support one type of extra content (in top bar) :"
            + tab_org_path
        )
        shutil.rmtree(output_path, ignore_errors=True)
        return None

    def get_tab_path_and_name(self, tab_text, tab_href):
        # set tab_org_path based on tab_href
        if tab_href[-1] == "/":
            tab_org_path = tab_href[:-1].split("/")[-1]
        else:
            tab_org_path = tab_href.split("/")[-1]

        # default value for tab_name and tab_path
        tab_name = tab_text
        tab_path = None

        # check for paths in org_tab_path
        if tab_org_path == "course" or "courseware" in tab_org_path:
            tab_name = tab_text.replace(", current location", "")
            tab_path = "course/" + self.head_course_xblock.folder_name + "/index.html"
        elif "info" in tab_org_path:
            tab_name = tab_text.replace(", current location", "")
            tab_path = "/index.html"
        elif "wiki" in tab_org_path and self.add_wiki:
            self.wiki = MoocWiki(self)
            tab_path = f"{str(self.wiki.wiki_path)}/index.html"
        elif "forum" in tab_org_path and self.add_forum:
            self.forum = MoocForum(self)
            tab_path = "forum/index.html"
        elif ("wiki" not in tab_org_path) and ("forum" not in tab_org_path):
            # check if already in dict
            for _, val in self.course_tabs.items():
                if val == f"{tab_org_path}/index.html":
                    tab_path = val
                    break
            else:
                tab_path = self.annex_extra_page(tab_href, tab_org_path)
        return tab_name, tab_path

    def get_course_tabs(self):
        logger.info("Getting course tabs ...")
        content = self.instance_connection.get_page(self.course_url)
        if not content:
            logger.error("Failed to get course tabs")
            raise SystemExit(1)
        soup = BeautifulSoup(content, "lxml")
        course_tabs = (
            soup.find("ol", attrs={"class": "course-material"})
            or soup.find("ul", attrs={"class": "course-material"})
            or soup.find("ul", attrs={"class": "navbar-nav"})
            or soup.find("ol", attrs={"class": "course-tabs"})
        )
        if course_tabs is not None:
            for tab in course_tabs.find_all("li"):
                tab_name, tab_path = self.get_tab_path_and_name(
                    tab_text=tab.get_text(), tab_href=tab.find("a")["href"]
                )
                if tab_name is not None and tab_path is not None:
                    self.course_tabs[tab_name] = tab_path

    def annex(self):
        self.get_course_tabs()
        logger.info("Downloading content for extra pages ...")
        for page in self.annexed_pages:
            page["content"] = self.dl_dependencies(
                content=page["content"],
                output_path=page["output_path"],
                path_from_html="",
            )

        logger.info("Processing book lists")
        for item in self.book_lists:
            item["book_list"] = self.get_book_list(
                item["book_list"], item["output_path"]
            )

        # annex wiki if available
        if hasattr(self, "wiki"):
            logger.info("Annexing wiki ...")
            self.wiki.annex_wiki()

        # annex forum if available
        if hasattr(self, "forum"):
            logger.info("Annexing forum ...")
            self.forum.annex_forum()

    def download_and_get_filename(
        self, src, output_path, with_ext=None, filter_ext=None
    ):
        if with_ext:
            ext = with_ext
        else:
            ext = os.path.splitext(src.split("?")[0])[1]
        filename = hashlib.sha256(str(src).encode("utf-8")).hexdigest() + ext
        output_file = output_path.joinpath(filename)
        if filter_ext and ext not in filter_ext:
            return
        if not output_file.exists():
            self.download_file(
                prepare_url(src, self.instance_url), output_file,
            )
        return filename

    def download_images_from_html(self, html_body, output_path, path_from_html):
        imgs = html_body.xpath("//img")
        for img in imgs:
            if "src" in img.attrib:
                filename = self.download_and_get_filename(
                    src=img.attrib["src"], output_path=output_path
                )
                if filename:
                    img.attrib["src"] = f"{path_from_html}/{filename}"
                    if "style" in img.attrib:
                        img.attrib["style"] += " max-width:100%"
                    else:
                        img.attrib["style"] = " max-width:100%"
        return bool(imgs)

    def download_documents_from_html(self, html_body, output_path, path_from_html):
        anchors = html_body.xpath("//a")
        for anchor in anchors:
            if "href" in anchor.attrib:
                filename = self.download_and_get_filename(
                    src=anchor.attrib["href"],
                    output_path=output_path,
                    filter_ext=DOWNLOADABLE_EXTENSIONS,
                )
                if filename:
                    anchor.attrib["href"] = f"{path_from_html}/{filename}"
        return bool(anchors)

    def download_css_from_html(self, html_body, output_path, path_from_html):
        css_files = html_body.xpath("//link")
        for css in css_files:
            if "href" in css.attrib:
                filename = self.download_and_get_filename(
                    src=css.attrib["href"], output_path=output_path
                )
                if filename:
                    css.attrib["href"] = f"{path_from_html}/{filename}"
        return bool(css_files)

    def download_js_from_html(self, html_body, output_path, path_from_html):
        js_files = html_body.xpath("//script")
        for js in js_files:
            if "src" in js.attrib:
                filename = self.download_and_get_filename(
                    src=js.attrib["src"], output_path=output_path
                )
                if filename:
                    js.attrib["src"] = f"{path_from_html}/{filename}"
        return bool(js_files)

    def download_sources_from_html(self, html_body, output_path, path_from_html):
        sources = html_body.xpath("//source")
        for source in sources:
            if "src" in source.attrib:
                filename = self.download_and_get_filename(
                    src=source.attrib["src"], output_path=output_path
                )
                if filename:
                    source.attrib["src"] = f"{path_from_html}/{filename}"
        return bool(sources)

    def download_iframes_from_html(self, html_body, output_path, path_from_html):
        iframes = html_body.xpath("//iframe")
        for iframe in iframes:
            if "src" in iframe.attrib:
                src = iframe.attrib["src"]
                if "youtube" in src:
                    filename = self.download_and_get_filename(
                        src=src,
                        output_path=output_path,
                        with_ext=f".{self.video_format}",
                    )
                    if filename:
                        x = jinja(
                            None,
                            "video.html",
                            False,
                            format=self.video_format,
                            video_path=filename,
                            subs=[],
                            autoplay=self.autoplay,
                        )
                        iframe.getparent().replace(iframe, lxml.html.fromstring(x))
                elif ".pdf" in src:
                    filename = self.download_and_get_filename(
                        src=src, output_path=output_path
                    )
                    if filename:
                        iframe.attrib["src"] = f"{path_from_html}/{filename}"
        return bool(iframes)

    def handle_jump_to_paths(self, target_path):
        def check_descendants_and_return_path(xblock_extractor):
            if xblock_extractor.xblock_json["type"] in ["vertical", "course"]:
                return xblock_extractor.relative_path + "/index.html"
            if not xblock_extractor.descendants:
                return None
            return check_descendants_and_return_path(xblock_extractor.descendants[0])

        for xblock_extractor in self.xblock_extractor_objects:
            if (
                urllib.parse.urlparse(xblock_extractor.xblock_json["lms_web_url"]).path
                == target_path
            ):
                # we have a path match, we now check xblock type to redirect properly
                # Only vertical and course xblocks have HTMLs
                return check_descendants_and_return_path(xblock_extractor)

    def rewrite_internal_links(self, html_body, output_path):
        def relative_dots(self, output_path):
            relative_path = output_path.resolve().relative_to(self.build_dir.resolve())
            path_length = len(relative_path.parts)
            if path_length >= 5:
                # from a vertical, the root is 5 jumps deep
                return "../" * 5
            return "../" * path_length

        def update_root_relative_path(self, anchor, fixed_path, output_path):
            if fixed_path:
                anchor.attrib["href"] = relative_dots(output_path) + fixed_path
            else:
                anchor.attrib["href"] = self.instance_url + anchor.attrib["href"]

        anchors = html_body.xpath("//a")
        path_prefix = f"{self.instance_connection.instance_config['course_prefix']}{urllib.parse.unquote_plus(self.course_id)}"
        has_changed = False
        for anchor in anchors:
            if "href" not in anchor.attrib:
                continue
            src = urllib.parse.urlparse(anchor.attrib["href"])

            # ignore external links
            if src.netloc and src.netloc != self.instance_url:
                continue

            # fix absolute path
            if src.path.startswith("/"):
                update_root_relative_path(anchor, None, output_path)
                has_changed = True
                continue

            if src.path.startswith(path_prefix):
                if "jump_to" in src.path:
                    # handle jump to paths (to an xblock)
                    path_fixed = self.handle_jump_to_paths(src.path)
                    if not path_fixed:
                        # xblock may be one of those from which a vertical is consisted of
                        # thus check if the parent has the valid path
                        # we only need to check one layer deep as there's single layer of xblocks beyond vertical
                        path_fixed = self.handle_jump_to_paths(
                            str(pathlib.Path(src.path).parent)
                        )
                    update_root_relative_path(anchor, path_fixed, output_path)
                    has_changed = True
                else:
                    # handle tab paths
                    _, tab_path = self.get_tab_path_and_name(
                        tab_text="", tab_href=src.path
                    )
                    update_root_relative_path(anchor, tab_path, output_path)
                    has_changed = True
        return has_changed

    def dl_dependencies(self, content, output_path, path_from_html):
        html_body = lxml.html.fromstring(str(content))
        imgs = self.download_images_from_html(html_body, output_path, path_from_html)
        docs = self.download_documents_from_html(html_body, output_path, path_from_html)
        css_files = self.download_css_from_html(html_body, output_path, path_from_html)
        js_files = self.download_js_from_html(html_body, output_path, path_from_html)
        sources = self.download_sources_from_html(
            html_body, output_path, path_from_html
        )
        iframes = self.download_iframes_from_html(
            html_body, output_path, path_from_html
        )
        rewritten_links = self.rewrite_internal_links(html_body, output_path)
        if any([imgs, docs, css_files, js_files, sources, iframes, rewritten_links]):
            content = lxml.html.tostring(html_body, encoding="unicode")
        return content

    def get_favicon(self):
        """ get the favicon from the given URL for the instance or the fallback URL """

        favicon_fpath = self.build_dir.joinpath("favicon.png")

        # download the favicon
        for favicon_url in [
            self.instance_connection.instance_config["favicon_url"],
            "https://github.com/edx/edx-platform/raw/master/lms/static/images/favicon.ico",
        ]:
            try:
                save_large_file(favicon_url, favicon_fpath)
                logger.debug(f"Favicon successfully downloaded from {favicon_url}")
                break
            except Exception:
                logger.debug(f"Favicon not downloaded from {favicon_url}")

        # convert and resize
        convert_image(favicon_fpath, favicon_fpath, "PNG")
        resize_image(favicon_fpath, 48, allow_upscaling=True)

        if not favicon_fpath.exists():
            raise Exception("Favicon download failed")

    def get_content(self):
        """ download the content for the course """

        def clean_content(html_article):
            """ removes unwanted elements from homepage html """

            unwanted_elements = {
                "div": {"class": "dismiss-message"},
                "a": {"class": "action-show-bookmarks"},
                "button": {"class": "toggle-visibility-button"},
            }
            for element_type, attribute in unwanted_elements.items():
                element = html_article.find(element_type, attrs=attribute)
                if element:
                    element.decompose()

        # download favicon
        self.get_favicon()

        # get the course url and generate homepage
        logger.info("Getting homepage ...")
        content = self.instance_connection.get_page(self.course_url)
        if not content:
            logger.error("Error while getting homepage")
            raise SystemExit(1)
        self.build_dir.joinpath("home").mkdir(parents=True, exist_ok=True)
        soup = BeautifulSoup(content, "lxml")
        welcome_message = soup.find("div", attrs={"class": "welcome-message"})

        # there are multiple welcome messages
        if not welcome_message:
            info_articles = soup.find_all(
                "div", attrs={"class": re.compile("info-wrapper")}
            )
            if info_articles == []:
                self.has_homepage = False
            else:
                for article in info_articles:
                    clean_content(article)
                    article["class"] = "toggle-visibility-element article-content"
                    self.homepage_html.append(
                        self.dl_dependencies(
                            content=article.prettify(),
                            output_path=self.build_dir.joinpath("home"),
                            path_from_html="home",
                        )
                    )

        # there is a single welcome message
        else:
            clean_content(welcome_message)
            self.homepage_html.append(
                self.dl_dependencies(
                    content=welcome_message.prettify(),
                    output_path=self.build_dir.joinpath("home"),
                    path_from_html="home",
                )
            )

        # make xblock_extractor objects download their content
        logger.info("Getting content for supported xblocks ...")
        self.head_course_xblock.download(self.instance_connection)

    def s3_credentials_ok(self):
        logger.info("Testing S3 Optimization Cache credentials ...")
        self.s3_storage = KiwixStorage(self.s3_url_with_credentials)
        if not self.s3_storage.check_credentials(
            list_buckets=True, bucket=True, write=True, read=True, failsafe=True
        ):
            logger.error("S3 cache instance_connection error testing permissions.")
            logger.error(f"  Server: {self.s3_storage.url.netloc}")
            logger.error(f"  Bucket: {self.s3_storage.bucket_name}")
            logger.error(f"  Key ID: {self.s3_storage.params.get('keyid')}")
            logger.error(f"  Public IP: {get_public_ip()}")
            return False
        return True

    def download_from_cache(self, key, fpath, meta):
        """ whether it downloaded from S3 cache """

        filetype = "jpeg" if fpath.suffix in [".jpeg", ".jpg"] else fpath.suffix[1:]
        if not self.s3_storage.has_object(key) or not meta:
            return False
        meta_dict = {
            "version": meta,
            "optimizer_version": None
            if self.use_any_optimized_version
            else OPTIMIZER_VERSIONS[filetype],
        }
        if not self.s3_storage.has_object_matching(key, meta_dict):
            return False
        try:
            self.s3_storage.download_file(key, fpath)
        except Exception as exc:
            logger.error(f"{key} failed to download from cache: {exc}")
            return False
        logger.info(f"downloaded {fpath} from cache at {key}")
        return True

    def upload_to_cache(self, key, fpath, meta):
        """ whether it uploaded to S3 cache """

        filetype = "jpeg" if fpath.suffix in [".jpeg", ".jpg"] else fpath.suffix[1:]
        if not meta or not filetype:
            return False
        meta = {"version": meta, "optimizer_version": OPTIMIZER_VERSIONS[filetype]}
        try:
            self.s3_storage.upload_file(fpath, key, meta=meta)
        except Exception as exc:
            logger.error(f"{key} failed to upload to cache: {exc}")
            return False
        logger.info(f"uploaded {fpath} to cache at {key}")
        return True

    def downlaod_form_url(self, url, fpath, filetype):
        download_path = fpath
        if (
            filetype
            and (fpath.suffix[1:] != filetype)
            and not (filetype == "jpg" and fpath.suffix[1:] == "jpeg")
        ):
            download_path = pathlib.Path(
                tempfile.NamedTemporaryFile(
                    suffix=f".{filetype}", dir=fpath.parent, delete=False
                ).name
            )
        try:
            save_large_file(url, download_path)
            return download_path
        except Exception as exc:
            logger.error(f"Error while running save_large_file(): {exc}")
            if download_path.exists() and download_path.is_file():
                os.unlink(download_path)
            return None

    def download_from_youtube(self, url, fpath):
        audext, vidext = {"webm": ("webm", "webm"), "mp4": ("m4a", "mp4")}[
            self.video_format
        ]
        output_file_name = fpath.name.replace(fpath.suffix, "")
        options = {
            "outtmpl": str(fpath.parent.joinpath(f"{output_file_name}.%(ext)s")),
            "preferredcodec": self.video_format,
            "format": f"best[ext={vidext}]/bestvideo[ext={vidext}]+bestaudio[ext={audext}]/best",
            "retries": 20,
            "fragment-retries": 50,
        }
        try:
            with youtube_dl.YoutubeDL(options) as ydl:
                ydl.download([url])
                for content in fpath.parent.iterdir():
                    if content.is_file() and content.name.startswith(
                        f"{output_file_name}."
                    ):
                        return content
        except Exception as exc:
            logger.error(f"Error while running youtube_dl: {exc}")
            return None

    def convert_video(self, src, dst):
        if (src.suffix[1:] != self.video_format) or self.low_quality:
            preset = VideoWebmLow() if self.video_format == "webm" else VideoMp4Low()
            return reencode(
                src, dst, preset.to_ffmpeg_args(), delete_src=True, failsafe=False,
            )

    def optimize_image(self, src, dst):
        optimized = False
        if src.suffix in [".jpeg", ".jpg"]:
            optimized = (
                exec_cmd("jpegoptim --strip-all -m50 " + str(src), timeout=10) == 0
            )
        elif src.suffix == ".png":
            exec_cmd(
                "pngquant --verbose --nofs --force --ext=.png " + str(src), timeout=10
            )
            exec_cmd("advdef -q -z -4 -i 5  " + str(src), timeout=50)
            optimized = True
        elif src.suffix == ".gif":
            optimized = exec_cmd("gifsicle --batch -O3 -i " + str(src), timeout=10) == 0
        if src.resolve() != dst.resolve():
            shutil.move(src, dst)
        return optimized

    def optimize_file(self, src, dst):
        if src.suffix[1:] in VIDEO_FORMATS:
            return self.convert_video(src, dst)
        if src.suffix[1:] in IMAGE_FORMATS:
            return self.optimize_image(src, dst)

    def generate_s3_key(self, url, fpath):
        if fpath.suffix[1:] in VIDEO_FORMATS:
            quality = "low" if self.low_quality else "high"
        else:
            quality = "default"
        src_url = urllib.parse.urlparse(url)
        prefix = f"{src_url.scheme}://{src_url.netloc}/"
        safe_url = f"{src_url.netloc}/{urllib.parse.quote_plus(src_url.geturl()[len(prefix):])}"
        # safe url looks similar to ww2.someplace.state.gov/data%2F%C3%A9t%C3%A9%2Fsome+chars%2Fimage.jpeg%3Fv%3D122%26from%3Dxxx%23yes
        return f"{fpath.suffix[1:]}/{safe_url}/{quality}"

    def download_file(self, url, fpath):
        is_youtube = "youtube" in url
        downloaded_from_cache = False
        meta, filetype = get_meta_from_url(url)
        if self.s3_storage:
            s3_key = self.generate_s3_key(url, fpath)
            downloaded_from_cache = self.download_from_cache(s3_key, fpath, meta)
        if not downloaded_from_cache:
            if is_youtube:
                downloaded_file = self.download_from_youtube(url, fpath)
            else:
                downloaded_file = self.downlaod_form_url(url, fpath, filetype)
            if not downloaded_file:
                logger.error(f"Error while downloading file from URL {url}")
                return
            try:
                optimized = self.optimize_file(downloaded_file, fpath)
                if self.s3_storage and optimized:
                    self.upload_to_cache(s3_key, fpath, meta)
            except Exception as exc:
                logger.error(f"Error while optimizing {fpath}: {exc}")
                return
            finally:
                if downloaded_file.resolve() != fpath.resolve() and not fpath.exists():
                    shutil.move(downloaded_file, fpath)

    def render_booknav(self):
        for book_nav in self.book_lists:
            jinja(
                book_nav["output_path"].joinpath("index.html"),
                "booknav.html",
                False,
                book_list=book_nav["book_list"],
                dir_path=book_nav["dir_path"],
                mooc=self,
                rooturl="../../../",
            )

    def render(self):
        # Render course
        self.head_course_xblock.render()

        # Render annexed pages
        for page in self.annexed_pages:
            jinja(
                page["output_path"].joinpath("index.html"),
                "specific_page.html",
                False,
                title=page["title"],
                mooc=self,
                content=page["content"],
                rooturl="../",
            )

        # render wiki if available
        if hasattr(self, "wiki"):
            self.wiki.render_wiki()

        # render forum if available
        if hasattr(self, "forum"):
            self.forum.render_forum()

        # render book lists
        if len(self.book_lists) != 0:
            self.render_booknav()
        if self.has_homepage:
            # render homepage
            jinja(
                self.build_dir.joinpath("index.html"),
                "home.html",
                False,
                messages=self.homepage_html,
                mooc=self,
                render_homepage=True,
            )
        shutil.copytree(
            ROOT_DIR.joinpath("templates").joinpath("assets"),
            self.build_dir.joinpath("assets"),
        )

    def get_zim_info(self):
        if not self.has_homepage:
            homepage = f"{self.head_course_xblock.relative_path}/index.html"
        else:
            homepage = "index.html"

        fallback_description = (
            self.course_info["short_description"]
            if self.course_info["short_description"]
            else f"{self.course_info['name']} from {self.course_info['org']}"
        )

        return {
            "description": self.description
            if self.description
            else fallback_description,
            "title": self.title if self.title else self.course_info["name"],
            "creator": self.creator if self.creator else self.course_info["org"],
            "homepage": homepage,
        }

    def run(self):
        logger.info(
            f"Starting {SCRAPER} with:\n"
            f"  Course URL: {self.course_url}\n"
            f"  Email ID: {self.email}"
        )
        logger.debug("Checking for missing binaries")
        check_missing_binary(self.no_zim)
        if self.s3_url_with_credentials and not self.s3_credentials_ok():
            raise ValueError("Unable to connect to Optimization Cache. Check its URL.")
        if self.s3_storage:
            logger.info(
                f"Using cache: {self.s3_storage.url.netloc} with bucket: {self.s3_storage.bucket_name}"
            )
        logger.info("Testing openedx instance credentials ...")
        self.instance_connection = InstanceConnection(
            self.course_url, self.email, self.password
        )
        self.instance_connection.establish_connection()
        jinja_init()
        self.prepare_mooc_data()
        self.parse_course_xblocks()
        self.annex()
        self.get_content()
        self.render()
        if not self.no_zim:
            self.fname = (
                self.fname or f"{self.name.replace(' ', '-')}_{{period}}.zim"
            ).format(period=datetime.datetime.now().strftime("%Y-%m"))
            logger.info("building ZIM file")
            zim_info = self.get_zim_info()
            if not self.output_dir.exists():
                self.output_dir.mkdir(parents=True)
            make_zim_file(
                build_dir=self.build_dir,
                fpath=self.output_dir.joinpath(self.fname),
                name=self.name,
                main_page=zim_info["homepage"],
                favicon="favicon.png",
                title=zim_info["title"],
                description=zim_info["description"],
                language="eng",
                creator=zim_info["creator"],
                publisher=self.publisher,
                tags=self.tags + ["_category:other", "openedx"],
                scraper=SCRAPER,
                without_fulltext_index=True if self.no_fulltext_index else False,
            )
            if not self.keep_build_dir:
                logger.info("Removing temp folder...")
                shutil.rmtree(self.build_dir, ignore_errors=True)
        logger.info("Done everything")
