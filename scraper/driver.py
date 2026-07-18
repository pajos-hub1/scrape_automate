from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from config import MOBILE_USER_AGENT

_driver_path = None


def build_driver(headless=True):
    global _driver_path

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=430,932")
    opts.add_argument(f"user-agent={MOBILE_USER_AGENT}")
    opts.add_argument("--lang=en-US")

    if _driver_path is None:
        _driver_path = ChromeDriverManager().install()

    service = Service(_driver_path)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(45)
    return driver
