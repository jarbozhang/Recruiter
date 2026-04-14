"""浏览器驱动工厂

根据配置自动选择 BrowserDriver 实现。
"""

from recruiter import config
from recruiter.browser.base import BrowserDriver


def create_driver() -> BrowserDriver:
    """根据 BROWSER_DRIVER 配置创建对应的浏览器驱动。"""
    driver_type = config.BROWSER_DRIVER.lower()

    if driver_type == "playwright":
        from recruiter.browser.playwright_driver import PlaywrightAdsPowerDriver
        return PlaywrightAdsPowerDriver(
            api_key=config.ADSPOWER_API_KEY,
            profile_id=config.ADSPOWER_PROFILE_ID,
            api_base=config.ADSPOWER_API_BASE,
        )
    elif driver_type == "selenium":
        from recruiter.browser.adspower import AdsPowerDriver
        return AdsPowerDriver(
            api_key=config.ADSPOWER_API_KEY,
            profile_id=config.ADSPOWER_PROFILE_ID,
            api_base=config.ADSPOWER_API_BASE,
        )
    elif driver_type == "bb-browser":
        from recruiter.browser.bb_browser import BBBrowserDriver
        return BBBrowserDriver()
    else:
        raise ValueError(f"Unknown browser driver: {driver_type}")
