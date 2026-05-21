"""
MS365 메일 본인 메일함 일괄 삭제 — Microsoft Graph + MSAL Device Code Flow.

흐름:
  acquire_token (MSAL device code, 캐시 재사용)
  GET /me → 로그인 사용자 확인
  GET /me/mailFolders/{folder}/messages (+ $filter / $orderby / $top, nextLink 페이지네이션)
  클라이언트 필터 (subject_contains, body_contains, keep_pattern)
  dry-run: 미리보기 / execute: move → deleteditems (또는 --hard 영구 삭제)

제한:
  - delegated 권한 (본인 메일함만)
  - Litigation Hold / Retention / eDiscovery 시 --hard 가 무력 (Recoverable Items 잔존)
  - Graph throttling — 429 시 Retry-After 자동 백오프
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from msal import PublicClientApplication, SerializableTokenCache

THIS_DIR = Path(__file__).resolve().parent
LOG_DIR = THIS_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
BACKUP_DIR = THIS_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
CACHE_PATH = THIS_DIR / ".token_cache.bin"

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite", "User.Read"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ms365_self_delete")


# ─────────────────────────────────────────────────────────────
# 인증
# ─────────────────────────────────────────────────────────────
def _load_cache() -> SerializableTokenCache:
    cache = SerializableTokenCache()
    if CACHE_PATH.exists():
        cache.deserialize(CACHE_PATH.read_text())
    return cache


def _save_cache(cache: SerializableTokenCache) -> None:
    if cache.has_state_changed:
        CACHE_PATH.write_text(cache.serialize())
        CACHE_PATH.chmod(0o600)


def acquire_token() -> str:
    client_id = os.environ.get("MS365_CLIENT_ID")
    tenant = os.environ.get("MS365_TENANT_ID", "common")
    if not client_id:
        log.error("MS365_CLIENT_ID 가 .env 에 없음")
        sys.exit(1)
    authority = f"https://login.microsoftonline.com/{tenant}"
    cache = _load_cache()
    app = PublicClientApplication(client_id, authority=authority, token_cache=cache)

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            log.error("device flow 실패: %s", flow)
            sys.exit(1)
        print("─" * 60)
        print(flow["message"])
        print("─" * 60)
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        log.error("인증 실패: %s", result.get("error_description", result))
        sys.exit(1)
    _save_cache(cache)
    return result["access_token"]


# ─────────────────────────────────────────────────────────────
# Graph 호출 — 429 자동 백오프
# ─────────────────────────────────────────────────────────────
def _graph_request(method: str, url: str, token: str, **kw) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(kw.pop("headers", {}))
    for attempt in range(5):
        r = requests.request(method, url, headers=headers, timeout=30, **kw)
        if r.status_code == 429 or r.status_code == 503:
            wait = int(r.headers.get("Retry-After", "10"))
            log.warning("throttle %s — sleep %ds (attempt %d/5)", r.status_code, wait, attempt + 1)
            time.sleep(wait + 1)
            continue
        return r
    return r  # 최종 응답 그대로


# ─────────────────────────────────────────────────────────────
# 폴더
# ─────────────────────────────────────────────────────────────
def list_folders(token: str) -> list[dict]:
    """본인 메일함의 모든 mailFolder 목록 (totalItemCount 포함)."""
    out: list[dict] = []
    url = f"{GRAPH}/me/mailFolders?$top=100&$select=id,displayName,totalItemCount,unreadItemCount"
    while url:
        r = _graph_request("GET", url, token)
        if r.status_code != 200:
            log.error("mailFolders fail: %s %s", r.status_code, r.text[:200])
            break
        data = r.json()
        out.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return out


# ─────────────────────────────────────────────────────────────
# 조회
# ─────────────────────────────────────────────────────────────
def list_messages(
    token: str,
    folder: str,
    since: str | None,
    until: str | None,
    from_addr: str | None,
    limit: int | None,
) -> list[dict]:
    """서버사이드 $filter — receivedDateTime / from. 페이지네이션."""
    filters: list[str] = []
    if since:
        filters.append(f"receivedDateTime ge {since}T00:00:00Z")
    if until:
        filters.append(f"receivedDateTime lt {until}T00:00:00Z")
    if from_addr:
        filters.append(f"from/emailAddress/address eq '{from_addr}'")

    params: dict[str, str] = {
        "$select": "id,subject,from,toRecipients,receivedDateTime,sentDateTime,bodyPreview,hasAttachments,internetMessageId,parentFolderId",
        "$top": "100",
        "$orderby": "receivedDateTime desc",
    }
    if filters:
        params["$filter"] = " and ".join(filters)

    url = f"{GRAPH}/me/mailFolders/{folder}/messages"
    out: list[dict] = []
    first = True
    while url:
        r = _graph_request("GET", url, token, params=params if first else None)
        first = False
        if r.status_code != 200:
            log.error("messages fail: %s %s", r.status_code, r.text[:300])
            break
        data = r.json()
        out.extend(data.get("value", []))
        if limit and len(out) >= limit:
            return out[:limit]
        url = data.get("@odata.nextLink")
        time.sleep(0.1)  # 페이지 간 미세 sleep
    return out


def filter_client_side(
    items: list[dict],
    subject_contains: str | None,
    body_contains: str | None,
    keep_pattern: str | None,
) -> list[dict]:
    out: list[dict] = []
    sc = (subject_contains or "").lower()
    bc = (body_contains or "").lower()
    for m in items:
        subj = (m.get("subject") or "")
        prev = (m.get("bodyPreview") or "")
        if sc and sc not in subj.lower():
            continue
        if bc and bc not in prev.lower():
            continue
        if keep_pattern and (keep_pattern in subj or keep_pattern in prev):
            continue
        out.append(m)
    return out


# ─────────────────────────────────────────────────────────────
# 백업
# ─────────────────────────────────────────────────────────────
def backup_messages(
    token: str,
    items: list[dict],
    folder_label: str,
    save_eml: bool,
) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = folder_label.replace("/", "_").replace(" ", "_")[:40]
    json_path = BACKUP_DIR / f"backup_{safe}_{stamp}.json"
    txt_path = BACKUP_DIR / f"backup_{safe}_{stamp}.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "folder": folder_label,
            "exported_at": datetime.now().isoformat(),
            "message_count": len(items),
            "messages": items,
        }, f, ensure_ascii=False, indent=2, default=str)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# MS365 메일 백업\n")
        f.write(f"# 폴더: {folder_label}\n")
        f.write(f"# 백업 시각: {datetime.now().isoformat()}\n")
        f.write(f"# 메시지 수: {len(items)}\n#\n")
        for m in sorted(items, key=lambda x: x.get("receivedDateTime") or ""):
            t = (m.get("receivedDateTime") or "")[:19].replace("T", " ")
            fr = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
            tos = ", ".join((t_.get("emailAddress") or {}).get("address", "") for t_ in (m.get("toRecipients") or []))[:80]
            subj = m.get("subject") or "(no subject)"
            prev = (m.get("bodyPreview") or "").replace("\n", " ").strip()[:200]
            att = " 📎" if m.get("hasAttachments") else ""
            f.write(f"[{t}] {fr} → {tos} | {subj}{att}\n  {prev}\n\n")

    log.info("백업: %s + %s (%d 통)", json_path.name, txt_path.name, len(items))

    if save_eml:
        eml_dir = BACKUP_DIR / f"eml_{safe}_{stamp}"
        eml_dir.mkdir(exist_ok=True)
        ok = 0
        for i, m in enumerate(items, 1):
            r = _graph_request("GET", f"{GRAPH}/me/messages/{m['id']}/$value", token)
            if r.status_code == 200:
                # 파일명: 시각_제목 (안전한 문자만)
                t = (m.get("receivedDateTime") or "")[:19].replace(":", "-").replace("T", "_")
                subj = "".join(c if c.isalnum() or c in " -_" else "_" for c in (m.get("subject") or "")[:60]).strip() or "no_subject"
                (eml_dir / f"{t}__{subj}.eml").write_bytes(r.content)
                ok += 1
            if i % 20 == 0:
                log.info("  .eml 진행: %d/%d", i, len(items))
            time.sleep(0.05)
        log.info("  .eml 저장: %d/%d → %s/", ok, len(items), eml_dir.name)

    return json_path, txt_path


# ─────────────────────────────────────────────────────────────
# 삭제
# ─────────────────────────────────────────────────────────────
def _delete_one(
    token: str,
    msg: dict,
    hard: bool,
    sleep_sec: float,
    log_lock: threading.Lock,
    log_fp,
) -> str:
    """결과: 'ok' / 'skip' / 'fail'."""
    msg_id = msg["id"]
    subj = (msg.get("subject") or "")[:80]

    if hard:
        r = _graph_request("DELETE", f"{GRAPH}/me/messages/{msg_id}", token)
        ok = r.status_code in (200, 204)
        not_found = r.status_code == 404
    else:
        r = _graph_request(
            "POST",
            f"{GRAPH}/me/messages/{msg_id}/move",
            token,
            headers={"Content-Type": "application/json"},
            json={"destinationId": "deleteditems"},
        )
        ok = r.status_code in (200, 201)
        not_found = r.status_code == 404

    if ok:
        result = "ok"
    elif not_found:
        result = "skip"
    else:
        result = "fail"

    with log_lock:
        if result == "ok":
            log.info("OK %s %r", msg_id[:30], subj)
        elif result == "skip":
            log.info("SKIP %s (404 not_found)", msg_id[:30])
        else:
            log.warning("FAIL %s status=%s body=%s", msg_id[:30], r.status_code, r.text[:200])
        log_fp.write(json.dumps({
            "msg_id": msg_id,
            "subject_head": subj,
            "from": (msg.get("from") or {}).get("emailAddress", {}).get("address"),
            "received_at": msg.get("receivedDateTime"),
            "action": "hard" if hard else "trash",
            "result": result,
            "status_code": r.status_code,
            "deleted_at": datetime.now().isoformat(),
        }, ensure_ascii=False) + "\n")
        log_fp.flush()

    time.sleep(sleep_sec)
    return result


def delete_messages(
    token: str,
    items: list[dict],
    hard: bool,
    sleep_sec: float,
    workers: int,
) -> dict:
    log_path = LOG_DIR / f"deleted_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    counts = {"ok": 0, "skip": 0, "fail": 0}
    lock = threading.Lock()
    per_worker_sleep = sleep_sec * workers if workers > 1 else sleep_sec

    log.info("삭제 시작: %d 통, workers=%d, mode=%s, per-worker sleep=%.2fs",
             len(items), workers, "hard(영구)" if hard else "trash(휴지통)", per_worker_sleep)

    with open(log_path, "w", encoding="utf-8") as f:
        if workers <= 1:
            for i, m in enumerate(items, 1):
                r = _delete_one(token, m, hard, per_worker_sleep, lock, f)
                counts[r] += 1
                if i % 20 == 0 or i == len(items):
                    log.info("진행: %d/%d", i, len(items))
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_delete_one, token, m, hard, per_worker_sleep, lock, f) for m in items]
                done = 0
                for fut in as_completed(futures):
                    counts[fut.result()] += 1
                    done += 1
                    if done % 20 == 0 or done == len(items):
                        with lock:
                            log.info("진행: %d/%d", done, len(items))

    log.info("로그 저장: %s", log_path)
    return counts


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MS365 본인 메일함 일괄 삭제 (Microsoft Graph)",
        epilog=(
            "예시:\n"
            "  --list-folders                                # 폴더 ID 확인\n"
            "  --folder inbox --from spam@x.com --dry-run    # spam 메일 미리보기\n"
            "  --folder inbox --since 2024-01-01 --until 2024-12-31 --backup --execute\n"
            "  --folder deleteditems --hard --execute        # 휴지통 비우기 (영구)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--list-folders", action="store_true", help="본인 메일함 폴더 목록 출력 후 종료")
    p.add_argument("--folder", default="inbox",
                   help="well-known: inbox|sentitems|drafts|deleteditems|archive | 또는 mailFolder ID")
    p.add_argument("--from", dest="from_addr", help="발신자 이메일 (정확히 일치)")
    p.add_argument("--subject-contains", dest="subject_contains", help="제목 부분 일치 (대소문자 무시)")
    p.add_argument("--body-contains", dest="body_contains", help="본문 미리보기 부분 일치 (전체 본문 X)")
    p.add_argument("--since", help="YYYY-MM-DD 이후")
    p.add_argument("--until", help="YYYY-MM-DD 이전")
    p.add_argument("--limit", type=int, help="최대 N개만 처리 (테스트)")
    p.add_argument("--keep-pattern", dest="keep_pattern", help="이 문자열 포함하면 보존")
    p.add_argument("--backup", action="store_true", help="삭제 전 JSON + TXT 백업")
    p.add_argument("--backup-only", action="store_true", help="백업만, 삭제 안 함")
    p.add_argument("--backup-eml", action="store_true", help="개별 .eml 도 함께 저장 (느림)")
    p.add_argument("--workers", type=int, default=1, help="병렬 worker (기본 1, 권장 1~5)")
    p.add_argument("--sleep", type=float, default=0.2, help="요청 간격(초). 기본 0.2")
    p.add_argument("--hard", action="store_true", help="영구 삭제 (DELETE) — 휴지통 안 거침. 정책상 차단 가능")
    p.add_argument("--execute", action="store_true", help="실제 실행 (기본은 dry-run)")
    args = p.parse_args()
    if not args.execute:
        args.dry_run = True
    else:
        args.dry_run = False
    return args


def main():
    load_dotenv(THIS_DIR / ".env")
    args = parse_args()
    token = acquire_token()

    me = _graph_request("GET", f"{GRAPH}/me", token).json()
    log.info("로그인: %s <%s>", me.get("displayName"), me.get("mail") or me.get("userPrincipalName"))

    if args.list_folders:
        folders = list_folders(token)
        log.info("폴더 %d 개:", len(folders))
        for f in sorted(folders, key=lambda x: -x.get("totalItemCount", 0)):
            print(f"  {f['id'][:60]:<60} | {f['displayName']:<30} ({f.get('totalItemCount', 0)} items, unread {f.get('unreadItemCount', 0)})")
        return

    log.info("대상 폴더: %s", args.folder)
    log.info("필터: from=%s subject_contains=%r body_contains=%r since=%s until=%s limit=%s keep=%r",
             args.from_addr, args.subject_contains, args.body_contains,
             args.since, args.until, args.limit, args.keep_pattern)

    # 0. 조회
    log.info("=== 1단계: 메일 조회 ===")
    items = list_messages(token, args.folder, args.since, args.until, args.from_addr, args.limit)
    log.info("서버 조회: %d 통", len(items))
    items = filter_client_side(items, args.subject_contains, args.body_contains, args.keep_pattern)
    log.info("클라이언트 필터 후: %d 통", len(items))

    if not items:
        log.info("(대상 없음)")
        return

    # 1. 미리보기
    log.info("=== 미리보기 (최근 5) ===")
    n_att = sum(1 for m in items if m.get("hasAttachments"))
    for m in items[:5]:
        t = (m.get("receivedDateTime") or "")[:16].replace("T", " ")
        fr = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
        subj = (m.get("subject") or "").strip()[:60]
        att = "📎" if m.get("hasAttachments") else "  "
        log.info("  %s  %-30s  %s  %s", t, fr[:30], att, subj)
    if len(items) > 5:
        log.info("  ... 총 %d 통, 첨부 포함 %d 통", len(items), n_att)

    # 2. 백업
    if args.backup or args.backup_only:
        log.info("=== 2단계: 백업 ===")
        backup_messages(token, items, args.folder, args.backup_eml)
        if args.backup_only:
            log.info("--backup-only 종료")
            return

    # 3. dry-run
    if args.dry_run:
        log.info("=== DRY-RUN — 삭제 X (--execute 추가 시 실제 실행) ===")
        log.info("  → %s 시뮬레이션: %d 통", "영구 삭제" if args.hard else "휴지통 이동", len(items))
        return

    # 4. 실행
    log.info("=== 3단계: 삭제 실행 ===")
    counts = delete_messages(token, items, args.hard, args.sleep, args.workers)
    log.info("완료: %s", counts)
    if args.hard and counts["ok"]:
        log.info("⚠ --hard 사용 — Litigation Hold 정책 시 Recoverable Items 폴더에 잔존 가능")


if __name__ == "__main__":
    main()
