import copy
import getpass
import http
import json
import sys
import urllib

from .constants import INSTANCE_CONFIGS, getLogger

logger = getLogger()


def get_instance_config(instance_netloc):
    if instance_netloc not in INSTANCE_CONFIGS:
        return INSTANCE_CONFIGS["default"].update(
            {"instance_url": f"https://{instance_netloc}"}
        )
    else:
        return INSTANCE_CONFIGS[instance_netloc]


def get_response(url, post_data, headers, max_attempts=2):
    req = urllib.request.Request(url, post_data, headers)
    for attempt in range(max_attempts):
        try:
            return urllib.request.urlopen(req).read().decode("utf-8")
        except Exception as exc:
            if attempt < max_attempts - 1:
                logger.debug(f"Error opening {url}: {exc}\nRetrying ...")
    logger.error(f"Max attempts exceeded for {url}")
    return {}


class InstanceConnection:
    def __init__(self, course_url, email, password):
        self.instance_netloc = urllib.parse.urlparse(course_url)[1]
        self.email = email
        self.password = password if password else getpass.getpass(stream=sys.stderr)

        self.instance_config = get_instance_config(self.instance_netloc)
        self.cookie_jar = http.cookiejar.LWPCookieJar("lol.cookies")
        self.headers = None
        self.instance_connection = None
        self.user = None

    def generate_connection_headers(self):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        opener.addheaders = [("User-Agent", "Mozilla/5.0")]
        urllib.request.install_opener(opener)
        opener.open(self.instance_config["instance_url"] + "/login")
        csrf_token = None
        for cookie in self.cookie_jar:
            if cookie.name == "csrftoken":
                csrf_token = cookie.value
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Referer": self.instance_config["instance_url"]
            + self.instance_config["login_page"],
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": csrf_token,
        }

    def establish_connection(self):
        self.generate_connection_headers()
        post_data = urllib.parse.urlencode(
            {"email": self.email, "password": self.password, "remember": False}
        ).encode("utf-8")
        # API login can also be used : /user_api/v1/account/login_session/
        self.instance_connection = self.get_api_json(
            self.instance_config["login_page"], post_data
        )
        if not self.instance_connection.get("success", False):
            raise SystemExit("Provided e-mail or password is incorrect")
        else:
            logger.info("Successfully logged in")
        for cookie in self.cookie_jar:
            if cookie.name == "edx-user-info" or cookie.name == "prod-edx-user-info":
                self.user = json.loads(cookie.value.replace(r"\054", ","))["username"]

    def get_api_json(self, page, post_data=None, referer=None):
        if "hint" in page:  # see with xblocks_extractor/Problem.py
            return {}
        headers = self.headers
        if referer:
            headers = copy.deepcopy(self.headers)
            headers["Referer"] = referer
        return json.loads(
            get_response(
                self.instance_config["instance_url"] + page, post_data, headers
            )
        )

    def get_page(self, url):
        headers = copy.deepcopy(self.headers)
        headers["X-Requested-With"] = ""
        return get_response(url, None, headers)

    def get_redirection(self, url):
        req = urllib.request.Request(url, None, self.headers)
        return urllib.request.urlopen(req).geturl()
