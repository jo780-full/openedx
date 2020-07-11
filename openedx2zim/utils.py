import html
import mimetypes
import pathlib
import re
import shlex
import subprocess
import urllib
import zlib

import requests

import jinja2
import mistune
from slugify import slugify
from webvtt import WebVTT

from .constants import ROOT_DIR, getLogger

logger = getLogger()


def exec_cmd(cmd, timeout=None):
    try:
        return subprocess.run(shlex.split(cmd), timeout=timeout)
    except Exception as exc:
        logger.error(exc)


def check_missing_binary(no_zim):
    """ check whether the required binaries are present on the system """

    def bin_is_present(binary):
        """ checks whether a given binary is present by running it """
        try:
            subprocess.run(
                binary, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError:
            return False
        return True

    if not no_zim and not bin_is_present("zimwriterfs"):
        logger.error("zimwriterfs is not available, please install it")
        raise SystemExit
    for binary in ["jpegoptim", "pngquant", "advdef", "gifsicle", "ffmpeg"]:
        if not bin_is_present(binary):
            logger.error(binary + " is not available, please install it")
            raise SystemExit


def markdown(text):
    renderer = mistune.HTMLRenderer()
    markdown = mistune.Markdown(renderer)
    return markdown(text)[3:-5].replace("\n", "<br>")


def remove_newline(text):
    return text.replace("\n", "")


def clean_top(t):
    return "/".join(t.split("/")[:-1])


def first_word(text):
    return " ".join(text.split(" ")[0:5])


def download_and_convert_subtitles(output_path, subtitles, instance_connection):
    processed_subtitles = {}
    for lang in subtitles:
        subtitle_file = pathlib.Path(output_path).joinpath(f"{lang}.vtt")
        if not subtitle_file.exists():
            try:
                raw_subtitle = instance_connection.get_page(subtitles[lang])
                subtitle = html.unescape(
                    re.sub(r"^0$", "1", str(raw_subtitle), flags=re.M)
                )
                with open(subtitle_file, "w") as sub_file:
                    sub_file.write(subtitle)
                if not is_webvtt(subtitle_file):
                    webvtt = WebVTT().from_srt(subtitle_file)
                    webvtt.save()
                processed_subtitles[lang] = f"{lang}.vtt"
            except urllib.error.HTTPError as exc:
                if exc.code == 404 or exc.code == 403:
                    logger.error(f"Failed to get subtitle from {subtitles[lang]}")
            except Exception as exc:
                logger.error(
                    f"Error while converting subtitle {subtitles[lang]} : {exc}"
                )
        else:
            processed_subtitles[lang] = f"{lang}.vtt"
    return processed_subtitles


def is_webvtt(subtitle_file):
    with open(subtitle_file, "r") as sub_file:
        first_line = sub_file.readline()
    return "webvtt" in first_line.lower()


def jinja_init():
    global ENV
    templates = ROOT_DIR.joinpath("templates")
    template_loader = jinja2.FileSystemLoader(searchpath=templates)
    ENV = jinja2.Environment(loader=template_loader, autoescape=True)
    filters = dict(
        slugify=slugify,
        markdown=markdown,
        remove_newline=remove_newline,
        first_word=first_word,
        clean_top=clean_top,
    )
    ENV.filters.update(filters)


def jinja(output, template, deflate, **context):
    template = ENV.get_template(template)
    page = template.render(**context, output_path=str(output))
    if not output:
        return page
    with open(output, "w") as html_page:
        if deflate:
            html_page.write(zlib.compress(page.encode("utf-8")))
        else:
            html_page.write(page)


def get_meta_from_url(url):
    def get_response_headers(url):
        for attempt in range(5):
            try:
                return requests.head(url=url, allow_redirects=True, timeout=30).headers
            except requests.exceptions.Timeout:
                logger.error(f"{url} > HEAD request timed out ({attempt})")
        raise Exception("Max retries exceeded")

    try:
        response_headers = get_response_headers(url)
    except Exception as exc:
        logger.error(f"{url} > Problem with head request\n{exc}\n")
        return None, None
    else:
        content_type = mimetypes.guess_extension(
            response_headers.get("content-type", None).split(";", 1)[0].strip()
        )[1:]
        if response_headers.get("etag", None) is not None:
            return response_headers["etag"], content_type
        if response_headers.get("last-modified", None) is not None:
            return response_headers["last-modified"], content_type
        if response_headers.get("content-length", None) is not None:
            return response_headers["content-length"], content_type
    return None, content_type
