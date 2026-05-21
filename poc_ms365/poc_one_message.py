"""
MS365 메일 - 1통 동작 확인 PoC.

목표:
  1) Azure AD 앱 등록 + Device Code Flow 인증
  2) 받은편지함의 최신 메일 1통 fetch (제목 / from / preview)
  3) --delete 옵션 시 휴지통 이동 (영구 삭제 X — 「삭제된 항목」 폴더에 남음)
  4) --hard 옵션 시 휴지통의 1통 영구 삭제 (Recoverable Items 잔존 — 정책에 따라)

이게 되면 본격 MVP (Slack 도구와 동등한 백업/필터/병렬) 진행.
"""
from __future__ import annotations
import os
import sys
import argparse
from pathlib import Path

import requests
from dotenv import load_dotenv
from msal import PublicClientApplication

load_dotenv(Path(__file__).parent / ".env")

CLIENT_ID = os.environ.get("MS365_CLIENT_ID")
TENANT = os.environ.get("MS365_TENANT_ID", "common")
SCOPES = ["Mail.ReadWrite", "User.Read"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}"
GRAPH = "https://graph.microsoft.com/v1.0"
CACHE_PATH = Path(__file__).parent / ".token_cache.bin"


def _load_cache():
    from msal import SerializableTokenCache
    cache = SerializableTokenCache()
    if CACHE_PATH.exists():
        cache.deserialize(CACHE_PATH.read_text())
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        CACHE_PATH.write_text(cache.serialize())
        CACHE_PATH.chmod(0o600)


def get_token() -> str:
    if not CLIENT_ID:
        sys.exit("❌ .env 에 MS365_CLIENT_ID 가 없음")
    cache = _load_cache()
    app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            sys.exit(f"❌ device flow 실패: {flow}")
        print("─" * 60)
        print(flow["message"])
        print("─" * 60)
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        sys.exit(f"❌ 인증 실패: {result.get('error_description', result)}")
    _save_cache(cache)
    return result["access_token"]


def main():
    parser = argparse.ArgumentParser(description="MS365 1통 PoC")
    parser.add_argument("--folder", default="inbox", help="대상 폴더 (inbox|sentitems|drafts|deleteditems)")
    parser.add_argument("--delete", action="store_true", help="휴지통 이동 (영구 삭제 X)")
    parser.add_argument("--hard", action="store_true", help="영구 삭제 (DELETE — 정책상 차단 가능)")
    args = parser.parse_args()

    token = get_token()
    H = {"Authorization": f"Bearer {token}"}

    me = requests.get(f"{GRAPH}/me", headers=H, timeout=10).json()
    print(f"로그인: {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>")
    print()

    print(f"=== 최신 메일 1통 조회 (폴더={args.folder}) ===")
    r = requests.get(
        f"{GRAPH}/me/mailFolders/{args.folder}/messages",
        headers=H,
        params={"$top": 1, "$select": "id,subject,from,receivedDateTime,bodyPreview,hasAttachments"},
        timeout=10,
    )
    if r.status_code != 200:
        sys.exit(f"❌ 조회 실패 status={r.status_code} body={r.text[:300]}")
    items = r.json().get("value", [])
    if not items:
        print("(메시지 없음)")
        return
    m = items[0]
    msg_id = m["id"]
    sender = m.get("from", {}).get("emailAddress", {}).get("address", "?")
    print(f"  id          : {msg_id[:50]}...")
    print(f"  receivedAt  : {m.get('receivedDateTime')}")
    print(f"  from        : {sender}")
    print(f"  subject     : {m.get('subject', '(no subject)')}")
    print(f"  attachments : {m.get('hasAttachments')}")
    preview = (m.get("bodyPreview") or "").replace("\n", " ").strip()
    print(f"  preview     : {preview[:100]}")
    print()

    if args.hard:
        print("=== 영구 삭제 (DELETE) ===")
        rr = requests.delete(f"{GRAPH}/me/messages/{msg_id}", headers=H, timeout=10)
        if rr.status_code in (200, 204):
            print(f"  ✅ DELETE 완료 (status={rr.status_code})")
            print("  ⚠ Litigation Hold 적용 시 Recoverable Items 폴더에 잔존할 수 있음")
        else:
            print(f"  ❌ 실패 status={rr.status_code} body={rr.text[:300]}")
    elif args.delete:
        print("=== 휴지통 이동 (move → deleteditems) ===")
        rr = requests.post(
            f"{GRAPH}/me/messages/{msg_id}/move",
            headers={**H, "Content-Type": "application/json"},
            json={"destinationId": "deleteditems"},
            timeout=10,
        )
        if rr.status_code in (200, 201):
            new_id = rr.json().get("id", "?")
            print(f"  ✅ 휴지통 이동 완료 (새 id: {new_id[:40]}...)")
            print("  복구: Outlook → 「삭제된 항목」 폴더에서 우클릭 → 「폴더로 이동」")
        else:
            print(f"  ❌ 실패 status={rr.status_code} body={rr.text[:300]}")
    else:
        print("(다음 단계: --delete 휴지통 이동 / --hard 영구 삭제)")


if __name__ == "__main__":
    main()
