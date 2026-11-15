from __future__ import annotations

import stat
from http.cookiejar import Cookie

import pytest

from ig_transcriber import login, pipeline


def _cookie(name: str, value: str, domain: str = ".instagram.com") -> Cookie:
    return Cookie(0, name, value, None, False, domain, True, True, "/", True, True, 1993456000, False, None, None, {})


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("REELRECON_HOME", str(tmp_path))
    for name in ("REELRECON_COOKIES_FILE", "REELRECON_COOKIES_FROM_BROWSER", "IG_TRANSCRIBER_COOKIES_FILE", "IG_TRANSCRIBER_COOKIES_FROM_BROWSER"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_save_session_is_private_and_used_by_pipeline(home):
    assert pipeline.session_source() is None
    path = login.save_session([_cookie("sessionid", "s3cret"), _cookie("csrftoken", "tok")])
    assert path == home / "instagram_cookies.txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert pipeline.session_source() == "saved_login"
    assert pipeline.cookie_settings() == (str(path), None)
    assert "sessionid=s3cret" in pipeline._instagram_cookie_header()


def test_env_overrides_saved_login(home, monkeypatch):
    login.save_session([_cookie("sessionid", "s")])
    monkeypatch.setenv("REELRECON_COOKIES_FROM_BROWSER", "firefox")
    assert pipeline.session_source() == "env_browser"
    assert pipeline.cookie_settings() == (None, "firefox")


def test_save_session_requires_sessionid(home):
    with pytest.raises(login.LoginError, match="sessionid"):
        login.save_session([_cookie("csrftoken", "tok")])
    assert not (home / "instagram_cookies.txt").exists()


def test_read_cookies_file_keeps_only_instagram(home, tmp_path):
    export = tmp_path / "export.txt"
    export.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1993456000\tsessionid\tabc\n"
        ".example.com\tTRUE\t/\tFALSE\t1993456000\tother\tx\n",
        encoding="utf-8",
    )
    cookies = login.read_cookies_file(export)
    assert [c.name for c in cookies] == ["sessionid"]
    with pytest.raises(login.LoginError):
        login.read_cookies_file(tmp_path / "missing.txt")


def test_login_removes_session_instagram_rejects(home, monkeypatch):
    monkeypatch.setattr(login, "read_browser_cookies", lambda browser, profile=None: [_cookie("sessionid", "stale")])

    def reject():
        raise login.LoginError("expired")

    monkeypatch.setattr(login, "verify_session", reject)
    assert login.main(["--browser", "chrome", "--no-open"]) == 1
    assert not (home / "instagram_cookies.txt").exists()

    monkeypatch.setattr(login, "verify_session", lambda: None)
    assert login.main(["--browser", "chrome", "--no-open"]) == 0
    assert (home / "instagram_cookies.txt").exists()
    assert login.main(["--logout"]) == 0
    assert not (home / "instagram_cookies.txt").exists()


PROFILE_HTML = (
    '<script>{"viewer":{"id":"111"}} "LSD",[],{"token":"lsd-tok"} "DTSGInitialData",[],{"token":"dtsg-tok"} '
    '{"username":"nasa","full_name":"NASA","id":"528817151","is_private":false}</script>'
)
REELS_RESPONSE = (
    'for (;;);{"data":{"fetch__XDTUserDict":{"edges":['
    '{"node":{"media":{"code":"PIN1","product_type":"clips","play_count":9,"like_count":1,"comment_count":0,"clips_tab_pinned_user_ids":["528817151"]}}},'
    '{"node":{"media":{"code":"NEW1","product_type":"clips","play_count":7637435,"like_count":107621,"comment_count":1338,"clips_tab_pinned_user_ids":[]}}}'
    '],"page_info":{"has_next_page":true}}}}'
)


def test_profile_scan_requires_login(home):
    with pytest.raises(pipeline.PipelineError, match="reelrecon login"):
        pipeline.fetch_profile_reels("nasa", "https://www.instagram.com/nasa/")


def test_profile_scan_reads_reels_via_graphql(home, monkeypatch):
    login.save_session([_cookie("sessionid", "s"), _cookie("ds_user_id", "111"), _cookie("csrftoken", "tok")])
    sent = []

    def fake_request(request, what):
        sent.append(request)
        return (PROFILE_HTML if request.get_method() == "GET" else REELS_RESPONSE).encode()

    monkeypatch.setattr(pipeline, "_instagram_request", fake_request)
    candidates = pipeline.collect_instagram_profile_videos("https://www.instagram.com/nasa/")
    assert [c.video_id for c in candidates] == ["NEW1"]  # pinned reel skipped
    assert candidates[0].metrics == {"views": 7637435, "likes": 107621, "comments": 1338, "duration_seconds": None}
    assert candidates[0].video_url == "https://www.instagram.com/reel/NEW1/"
    body = sent[1].data.decode()
    assert "528817151" in body and "lsd=lsd-tok" in body and "fb_dtsg=dtsg-tok" in body


def test_profile_scan_detects_expired_login(home, monkeypatch):
    login.save_session([_cookie("sessionid", "s"), _cookie("ds_user_id", "999")])
    monkeypatch.setattr(pipeline, "_instagram_request", lambda request, what: PROFILE_HTML.encode())
    with pytest.raises(pipeline.PipelineError, match="expired"):
        pipeline.fetch_profile_reels("nasa", "https://www.instagram.com/nasa/")


def test_profile_scan_reports_rotated_query(home, monkeypatch):
    login.save_session([_cookie("sessionid", "s"), _cookie("ds_user_id", "111")])
    monkeypatch.setattr(
        pipeline, "_instagram_request",
        lambda request, what: (PROFILE_HTML if request.get_method() == "GET" else "<!DOCTYPE html><html>").encode(),
    )
    with pytest.raises(pipeline.PipelineError, match="REELRECON_REELS_DOC_ID"):
        pipeline.fetch_profile_reels("nasa", "https://www.instagram.com/nasa/")


def test_profile_scan_refuses_private_accounts(home, monkeypatch):
    login.save_session([_cookie("sessionid", "s"), _cookie("ds_user_id", "111")])
    private = PROFILE_HTML.replace('"is_private":false', '"is_private":true')
    monkeypatch.setattr(pipeline, "_instagram_request", lambda request, what: private.encode())
    with pytest.raises(pipeline.PipelineError, match="private"):
        pipeline.fetch_profile_reels("nasa", "https://www.instagram.com/nasa/")
