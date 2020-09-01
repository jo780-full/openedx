import pathlib
import re
import urllib

import xxhash
import lxml.html
from bs4 import BeautifulSoup

from .constants import DOWNLOADABLE_EXTENSIONS, AUDIO_FORMATS
from .utils import jinja, prepare_url, get_back_jumps, remove_autogenerated_tags


class HtmlProcessor:
    def __init__(self, scraper):
        self.scraper = scraper

    def download_and_get_filename(
        self,
        src,
        output_path,
        netloc,
        path_on_server,
        with_ext=None,
        filter_ext=None,
    ):
        """downloads a file from src and return the name of the downloaded file

        with_ext: ensure that downloaded file has the given extension
        filter_ext: download only if the file to download has an extension in this list"""

        server_path = pathlib.Path(urllib.parse.urlparse(src).path)
        ext = with_ext if with_ext else server_path.suffix

        if server_path.suffix:
            filename = server_path.with_suffix(ext).name
        else:
            filename = xxhash.xxh64(str(src).encode("utf-8")).hexdigest() + ext

        output_file = output_path.joinpath(filename)
        if filter_ext and ext not in filter_ext:
            return None, None
        fresh_download = False
        if not output_file.exists():
            if self.scraper.download_file(
                prepare_url(src, netloc, path_on_server),
                output_file,
            ):
                fresh_download = True
            else:
                return None, None
        return filename, fresh_download

    def download_dependencies_from_css(
        self, css_org_url, css_path, output_path_from_css, netloc, path_on_server
    ):
        """Download all dependencies from CSS file contained in url() recursively

        - css_org_url: URL to the CSS file on the internet
        - css_path: path of CSS on the filesystem (Path)
        - output_path_from_css: string representing path of the output directory relative to css_path"""

        def encapsulate(url):
            return f"url({url})"

        def remove_quotes(url):
            if url[0] and url[-1] == "'":
                url = url[1:-1]
            if url[0] and url[-1] == '"':
                url = url[1:-1]
            return url

        # ensure the original CSS url has netloc
        css_org_url = prepare_url(css_org_url, netloc, path_on_server)
        css_org_url = urllib.parse.urlparse(css_org_url)

        with open(css_path, "r") as fp:
            content = fp.read()

        # split whole content on `url()` pattern to retrieve a list composed of
        # alternatively pre-pattern text and inside url() –– actual target text
        parts = re.split(r"url\((.+?)\)", content)
        for index, _ in enumerate(parts):
            if index % 2 == 0:  # skip even lines (0, 2, ..) as those are CSS code
                continue
            css_url = parts[index]  # css_urls are on odd lines

            # remove potential quotes (can be none, single or double)
            css_url = remove_quotes(css_url)

            # don't rewrite data: and empty URLs
            if re.match(r"^(://|data:|#)", css_url):
                parts[index] = encapsulate(css_url)
                continue

            # add netloc if not present
            parsed_url = urllib.parse.urlparse(css_url)
            if parsed_url.netloc == "":
                if parsed_url.path.startswith("/"):
                    css_url = (
                        css_org_url.netloc if css_org_url.netloc else netloc
                    ) + css_url
                else:
                    path_prefix = pathlib.Path(css_org_url.path)
                    if path_prefix.suffix != "":
                        path_prefix = path_prefix.parent
                    css_url = css_org_url.netloc + str(path_prefix.joinpath(css_url))

            output_path = (
                css_path.parent
                if not output_path_from_css
                else css_path.joinpath(output_path_from_css)
            )

            # download imported css files recursively
            if parts[index - 1].endswith("@import "):
                filename, _ = self.download_and_get_filename(
                    css_url,
                    output_path,
                    netloc=netloc,
                    path_on_server=path_on_server,
                    with_ext=".css",
                )
                parsed_css_url = urllib.parse.urlparse(css_url)
                self.download_dependencies_from_css(
                    css_org_url=css_url,
                    css_path=output_path.joinpath(filename),
                    output_path_from_css="",
                    netloc=parsed_css_url.netloc,
                    path_on_server=str(pathlib.Path(parsed_css_url.path).parent),
                )

            else:
                # download the file
                filename, _ = self.download_and_get_filename(
                    css_url, output_path, netloc=netloc, path_on_server=path_on_server
                )
            fixed = (
                filename
                if not output_path_from_css
                else f"{output_path_from_css}/{filename}"
            )
            parts[index] = encapsulate(fixed)

        with open(css_path, "w") as fp:
            fp.write("".join(parts))

    def download_images_from_html(
        self, html_body, output_path, path_from_html, netloc, path_on_server
    ):
        """ download images from <img> tag and fix path """

        imgs = html_body.xpath("//img")
        for img in imgs:
            if "src" in img.attrib:
                filename, _ = self.download_and_get_filename(
                    src=img.attrib["src"],
                    output_path=output_path,
                    netloc=netloc,
                    path_on_server=path_on_server,
                )
                if filename:
                    img.attrib["src"] = (
                        f"{filename}"
                        if not path_from_html
                        else f"{path_from_html}/{filename}"
                    )
                    if "style" in img.attrib:
                        img.attrib["style"] += " max-width:100%"
                    else:
                        img.attrib["style"] = " max-width:100%"
        return bool(imgs)

    def get_root_from_asset(self, path_from_html, root_from_html):
        """ get path to root from the downloaded/generated asset """

        # return original root if path_from_html is empty
        if path_from_html == "":
            return root_from_html

        nb_jumps_root_from_html = root_from_html.count("../")
        nb_back_jumps_output_path = path_from_html.count("../")

        # the path to the asset from HTML, minus the back jumps
        path_without_back_jumps = path_from_html[
            (nb_back_jumps_output_path) * len("../") :
        ]

        return get_back_jumps(
            nb_jumps_root_from_html
            - nb_back_jumps_output_path
            + len(pathlib.Path(path_without_back_jumps).parts)
        )

    def download_documents_from_html(
        self,
        html_body,
        output_path,
        path_from_html,
        root_from_html,
        netloc,
        path_on_server,
    ):
        """ download documents from <a> tag and fix path """

        anchors = html_body.xpath("//a")
        for anchor in anchors:
            if "href" in anchor.attrib:
                filename, _ = self.download_and_get_filename(
                    src=anchor.attrib["href"],
                    output_path=output_path,
                    netloc=netloc,
                    path_on_server=path_on_server,
                    filter_ext=DOWNLOADABLE_EXTENSIONS,
                )
                if filename:
                    file_format = pathlib.Path(filename).suffix[1:]
                    if file_format in AUDIO_FORMATS:
                        html_fpath = output_path.joinpath(
                            f"{filename.split('.')[0]}.html"
                        )
                        if not html_fpath.exists():
                            jinja(
                                html_fpath,
                                "audio_player.html",
                                False,
                                audio_path=filename,
                                path_to_root=self.get_root_from_asset(
                                    path_from_html, root_from_html
                                ),
                                audio_format=file_format,
                            )
                        filename = html_fpath.name
                    anchor.attrib["href"] = (
                        f"{filename}"
                        if not path_from_html
                        else f"{path_from_html}/{filename}"
                    )
        return bool(anchors)

    def get_path_and_netloc_to_send(self, netloc, path_on_server, downloaded_asset_url):
        """get the path and netloc to send recursively after downloading asset from downloaded_asset_url
        path_on_server is the current path on server and netloc is the current netloc"""

        parsed_src = urllib.parse.urlparse(downloaded_asset_url)
        path_recursive = path_on_server
        if parsed_src.path:
            asset_path_on_server = pathlib.Path(parsed_src.path)
            path_recursive = (
                asset_path_on_server
                if not asset_path_on_server.suffix
                else asset_path_on_server.parent
            )
            path_recursive = (
                str(path_recursive)
                if not parsed_src.path.startswith("/")
                else str(pathlib.Path(path_on_server).joinpath(path_recursive))
            )
        netloc_recursive = parsed_src.netloc if parsed_src.netloc else netloc
        return path_recursive, netloc_recursive

    def download_css_from_html(
        self, html_body, output_path, path_from_html, netloc, path_on_server
    ):
        """ download css files from <link> tag and fix path """

        css_files = html_body.xpath("//link")
        for css in css_files:
            if "href" in css.attrib:
                filename, fresh_download = self.download_and_get_filename(
                    src=css.attrib["href"],
                    output_path=output_path,
                    netloc=netloc,
                    path_on_server=path_on_server,
                )
                if filename:
                    if fresh_download:
                        (
                            path_recursive,
                            netloc_recursive,
                        ) = self.get_path_and_netloc_to_send(
                            netloc, path_on_server, css.attrib["href"]
                        )
                        self.download_dependencies_from_css(
                            css_org_url=css.attrib["href"],
                            css_path=output_path.joinpath(filename),
                            output_path_from_css="",
                            netloc=netloc_recursive,
                            path_on_server=path_recursive,
                        )
                    css.attrib["href"] = (
                        f"{filename}"
                        if not path_from_html
                        else f"{path_from_html}/{filename}"
                    )
        return bool(css_files)

    def download_js_from_html(
        self, html_body, output_path, path_from_html, netloc, path_on_server
    ):
        """ download javascript from <script> tag and fix path """

        js_files = html_body.xpath("//script")
        for js in js_files:
            if "src" in js.attrib:
                filename, _ = self.download_and_get_filename(
                    src=js.attrib["src"],
                    output_path=output_path,
                    netloc=netloc,
                    path_on_server=path_on_server,
                )
                if filename:
                    js.attrib["src"] = (
                        f"{filename}"
                        if not path_from_html
                        else f"{path_from_html}/{filename}"
                    )
        return bool(js_files)

    def download_sources_from_html(
        self, html_body, output_path, path_from_html, netloc, path_on_server
    ):
        """ downloads content from <source> tags """

        sources = html_body.xpath("//source")
        for source in sources:
            if "src" in source.attrib:
                filename, _ = self.download_and_get_filename(
                    src=source.attrib["src"],
                    output_path=output_path,
                    netloc=netloc,
                    path_on_server=path_on_server,
                )
                if filename:
                    source.attrib["src"] = (
                        f"{filename}"
                        if not path_from_html
                        else f"{path_from_html}/{filename}"
                    )
        return bool(sources)

    def download_iframes_from_html(
        self,
        html_body,
        output_path,
        path_from_html,
        root_from_html,
        netloc,
        path_on_server,
    ):
        """ download youtube videos and pdf files from iframes in html content """

        iframes = html_body.xpath("//iframe")
        for iframe in iframes:
            if "src" in iframe.attrib:
                src = iframe.attrib["src"]
                if "youtube" in src:
                    filename, _ = self.download_and_get_filename(
                        src=src,
                        output_path=output_path,
                        netloc=netloc,
                        path_on_server=path_on_server,
                        with_ext=f".{self.scraper.video_format}",
                    )
                    if filename:
                        x = jinja(
                            None,
                            "video.html",
                            False,
                            format=self.scraper.video_format,
                            video_path=f"{filename}"
                            if not path_from_html
                            else f"{path_from_html}/{filename}",
                            subs=[],
                            autoplay=self.scraper.autoplay,
                            path_to_root=root_from_html,
                            title="",
                        )
                        iframe.getparent().replace(iframe, lxml.html.fromstring(x))
                elif ".pdf" in src:
                    filename, _ = self.download_and_get_filename(
                        src=src,
                        output_path=output_path,
                        netloc=netloc,
                        path_on_server=path_on_server,
                    )
                    if filename:
                        iframe.attrib["src"] = (
                            f"{filename}"
                            if not path_from_html
                            else f"{path_from_html}/{filename}"
                        )
                else:
                    # handle iframe recursively
                    iframe_url = prepare_url(src, netloc)
                    src_content = self.scraper.instance_connection.get_page(iframe_url)
                    if not src_content:
                        continue
                    path_recursive, netloc_recursive = self.get_path_and_netloc_to_send(
                        netloc, path_on_server, iframe_url
                    )
                    modified_content = self.dl_dependencies_and_fix_links(
                        content=src_content,
                        output_path=output_path,
                        path_from_html="",
                        root_from_html=self.get_root_from_asset(
                            path_from_html, root_from_html
                        ),
                        netloc=netloc_recursive,
                        path_on_server=path_recursive,
                    )
                    filename = (
                        xxhash.xxh64(str(src).encode("utf-8")).hexdigest() + ".html"
                    )
                    fpath = output_path.joinpath(filename)
                    with open(fpath, "w") as html_file:
                        html_file.write(modified_content)
                    iframe.attrib["src"] = (
                        f"{filename}"
                        if not path_from_html
                        else f"{path_from_html}/{filename}"
                    )
        return bool(iframes)

    def handle_jump_to_paths(self, target_path):
        """ return a fixed path in zim for a inter-xblock path containing jump_to """

        def check_descendants_and_return_path(xblock_extractor):
            if xblock_extractor.xblock_json["type"] in ["vertical", "course"]:
                return xblock_extractor.relative_path + "/index.html"
            if not xblock_extractor.descendants:
                return None
            return check_descendants_and_return_path(xblock_extractor.descendants[0])

        for xblock_extractor in self.scraper.xblock_extractor_objects:
            if (xblock_extractor.xblock_json["block_id"] == target_path.parts[-1]) or (
                urllib.parse.urlparse(xblock_extractor.xblock_json["lms_web_url"]).path
                == str(target_path)
            ):
                # we have a path match, we now check xblock type to redirect properly
                # Only vertical and course xblocks have HTMLs
                return check_descendants_and_return_path(xblock_extractor)

    def rewrite_internal_links(self, html_body, root_from_html, netloc):
        """ rewrites internal links and ensures no root-relative links are left behind """

        def update_root_relative_path(anchor, fixed_path, root_from_html, netloc):
            """updates a root-relative path to the fixed path in zim
            if fixed path is not available, adds the instance url as its netloc"""

            if fixed_path:
                anchor.attrib["href"] = root_from_html + fixed_path
            else:
                anchor.attrib["href"] = netloc + anchor.attrib["href"]

        anchors = html_body.xpath("//a")
        path_prefix = f"{self.scraper.instance_config['course_prefix']}{urllib.parse.unquote_plus(self.scraper.course_id)}"
        has_changed = False
        for anchor in anchors:
            if "href" not in anchor.attrib:
                continue
            src = urllib.parse.urlparse(anchor.attrib["href"])

            # ignore external links
            if src.netloc and src.netloc != self.scraper.instance_url:
                continue

            # fix root-relative internal urls first
            if src.path.startswith(path_prefix):
                if "jump_to" in src.path and netloc:
                    # handle jump to paths (to an xblock)
                    src_path = pathlib.Path(src.path)
                    path_fixed = self.handle_jump_to_paths(src_path)
                    if not path_fixed:
                        # xblock may be one of those from which a vertical is consisted of
                        # thus check if the parent has the valid path
                        # we only need to check one layer deep as there's single layer of xblocks beyond vertical
                        path_fixed = self.handle_jump_to_paths(src_path.parent)
                    update_root_relative_path(
                        anchor, path_fixed, root_from_html, netloc
                    )
                    has_changed = True
                else:
                    # handle tab paths
                    _, tab_path = self.scraper.get_tab_path_and_name(
                        tab_text="", tab_href=src.path
                    )
                    update_root_relative_path(anchor, tab_path, root_from_html, netloc)
                    has_changed = True
                continue

            # fix root-relative path if not downloaded for zim
            if src.path.startswith("/"):
                update_root_relative_path(anchor, None, root_from_html, netloc)
                has_changed = True

        return has_changed

    def dl_dependencies_and_fix_links(
        self,
        content,
        output_path,
        path_from_html,
        root_from_html,
        netloc=None,
        path_on_server="",
    ):
        """ downloads all static dependencies from an HTML content, and fixes links """

        if not netloc:
            netloc = self.scraper.instance_url

        html_body = lxml.html.fromstring(str(content))
        imgs = self.download_images_from_html(
            html_body, output_path, path_from_html, netloc, path_on_server
        )
        docs = self.download_documents_from_html(
            html_body,
            output_path,
            path_from_html,
            root_from_html,
            netloc,
            path_on_server,
        )
        css_files = self.download_css_from_html(
            html_body, output_path, path_from_html, netloc, path_on_server
        )
        js_files = self.download_js_from_html(
            html_body, output_path, path_from_html, netloc, path_on_server
        )
        sources = self.download_sources_from_html(
            html_body,
            output_path,
            path_from_html,
            netloc,
            path_on_server,
        )
        iframes = self.download_iframes_from_html(
            html_body,
            output_path,
            path_from_html,
            root_from_html,
            netloc,
            path_on_server,
        )
        rewritten_links = self.rewrite_internal_links(html_body, root_from_html, netloc)
        if any([imgs, docs, css_files, js_files, sources, iframes, rewritten_links]):
            content = lxml.html.tostring(html_body, encoding="unicode")
        return content

    def defer_scripts(self, content, output_path, path_from_html):
        """ defer all scripts in content. For inline scripts, they're placed in a *.js file and deferred """

        soup = BeautifulSoup(content, "lxml")
        script_tags = soup.find_all("script")
        for script_tag in script_tags:
            if (
                script_tag.has_attr("type")
                and script_tag.attrs["type"] != "text/javascript"
                and script_tag.attrs["type"] != "application/javascript"
            ):
                continue

            if script_tag.has_attr("defer"):
                continue

            if script_tag.has_attr("src"):
                script_tag.attrs["defer"] = None
                continue

            if script_tag.string.strip():
                script_content = script_tag.string.strip()
                filename = f"{xxhash.xxh64(str(script_content[:200] if len(script_content) > 200 else script_content).encode('utf-8')).hexdigest()}.js"
                fpath = output_path.joinpath(filename)
                with open(fpath, "w") as fp:
                    fp.write(script_content)
                script_tag.string = ""
                script_tag.attrs["src"] = (
                    f"{filename}"
                    if not path_from_html
                    else f"{path_from_html}/{filename}"
                )
                script_tag.attrs["defer"] = None
        return str(soup)

    def extract_head_css_js(self, soup, output_path, path_from_html, root_from_html):
        """returns a list of processed html strings representing CSS and JS within the <head> element

        output_path: a Path object to store the downloaded CSS/JS to
        path_from_html: a string representing the path to output_path from the resultant HTML
        root_from_html: a string representing the path to the root from the resultant HTML"""

        html_headers = soup.find("head")
        head_css_js = (
            html_headers.find_all("script", recursive=False)
            + html_headers.find_all(
                "link", attrs={"rel": "stylesheet"}, recursive=False
            )
            + html_headers.find_all("style", recursive=False)
        )

        extra_head_content = []
        for header_element in head_css_js:
            extra_head_content.append(
                remove_autogenerated_tags(
                    self.dl_dependencies_and_fix_links(
                        content=str(header_element),
                        output_path=output_path,
                        path_from_html=path_from_html,
                        root_from_html=root_from_html,
                    )
                )
            )
        return extra_head_content

    def extract_body_end_scripts(
        self, soup, output_path, path_from_html, root_from_html
    ):
        """returns a list of processed html strings representing the <script> tags at the end of the <body>

        output_path: a Path object to store the downloaded CSS/JS to
        path_from_html: a string representing the path to output_path from the resultant HTML
        root_from_html: a string representing the path to the root from the resultant HTML"""

        html_body = soup.find("body")
        body_scripts = html_body.find_all("script", recursive=False)
        body_end_scripts = []
        for script in body_scripts:
            body_end_scripts.append(
                remove_autogenerated_tags(
                    self.dl_dependencies_and_fix_links(
                        content=str(script),
                        output_path=output_path,
                        path_from_html=path_from_html,
                        root_from_html=root_from_html,
                    )
                )
            )
        return body_end_scripts
