# mvp_ms365_self_delete

본인 MS365 메일함의 메시지를 **필터 조건에 맞춰 일괄 삭제** 하는 MVP. Slack 도구 (`mvp_slack_self_delete`) 와 동등한 옵션 세트.

## 한정 범위
- ✅ 본인 (`Mail.ReadWrite` delegated) 메일함만
- ✅ 모든 폴더 (Inbox / Sent / Drafts / Deleted / 사용자 정의)
- ✅ 휴지통 이동 (기본) 또는 영구 삭제 (`--hard`)
- ✅ 첨부 파일 자동 처리 (메일 삭제 시 종속)
- ❌ 다른 사용자 메일은 절대 접근 불가 (delegated 권한 한계)
- ❌ Litigation Hold / Retention / eDiscovery 적용 시 `--hard` 무력 (Recoverable Items 잔존)

## 필요 권한 (Delegated)
| Scope | 용도 |
|---|---|
| `Mail.ReadWrite` | 본인 메일 조회 + 삭제 + 이동 |
| `User.Read` | 로그인 사용자 정보 |
| `offline_access` | refresh token (자동 추가) |

## 사전 준비
1. **Azure 등록** — 본 저장소의 `poc_ms365/README.md` 의 § 1~3 절차 참조 (Application/Tenant ID 발급 + Mail.ReadWrite + Allow public client flows)
2. **PoC 1통 검증** — `poc_ms365/poc_one_message.py --delete` 가 정상 동작 후 본 MVP 진행 권장

## 환경 세팅

```bash
cd ~/a-projects/pii-cleaner/mvp_ms365_self_delete

cp .env.example .env
nano .env   # MS365_CLIENT_ID + MS365_TENANT_ID 채움

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

처음 실행 시 Device Code 표시 → 브라우저에서 인증. 이후 `.token_cache.bin` 재사용 (재인증 불필요).

## 사용

### 1) 폴더 목록 확인 (시작점)
```bash
python ms365_self_delete.py --list-folders
```
출력 예시:
```
AAMkAGI...        | Inbox                     (1247 items, unread 12)
AAMkAGI...        | Deleted Items             (380 items)
AAMkAGI...        | Sent Items                (892 items)
...
```

### 2) 필터 미리보기 (dry-run, 안전)
```bash
# 특정 발신자
python ms365_self_delete.py --folder inbox --from spam@example.com

# 기간 + 제목
python ms365_self_delete.py --folder inbox \
  --since 2024-01-01 --until 2024-06-30 \
  --subject-contains "광고"

# 보낸편지함의 회사 메일 (mailFolder ID 직접)
python ms365_self_delete.py --folder AAMkAGI... --from me@company.com
```

### 3) 백업만 (export 용)
```bash
# JSON + TXT
python ms365_self_delete.py --folder inbox --backup-only

# .eml 까지 (개별 메일 파일)
python ms365_self_delete.py --folder inbox --backup-only --backup-eml
```

### 4) 실제 삭제 — 휴지통 이동 (복구 가능)
```bash
python ms365_self_delete.py --folder inbox \
  --from spam@example.com \
  --backup --execute
```

### 5) 영구 삭제 (회복 불가)
```bash
python ms365_self_delete.py --folder deleteditems \
  --hard --backup --execute
```

> ⚠ Litigation Hold 정책 시 Recoverable Items 폴더에 30~14일 잔존. admin 외엔 못 봄.

### 6) 병렬 + 백업 (대규모)
```bash
python ms365_self_delete.py --folder inbox \
  --from newsletter@example.com \
  --backup --backup-eml --workers 5 --execute
```

## 옵션 정리

| 옵션 | 효과 |
|---|---|
| `--list-folders` | 폴더 목록 후 종료 |
| `--folder NAME` | inbox / sentitems / drafts / deleteditems / archive 또는 mailFolder ID |
| `--from EMAIL` | 발신자 일치 (서버 사이드) |
| `--subject-contains TEXT` | 제목 부분 일치 (대소문자 무시, 클라이언트) |
| `--body-contains TEXT` | bodyPreview 부분 일치 (전체 본문 X) |
| `--since YYYY-MM-DD` | 이후 |
| `--until YYYY-MM-DD` | 이전 |
| `--limit N` | 최대 N개 (테스트) |
| `--keep-pattern TEXT` | 포함 시 보존 |
| `--backup` | JSON + TXT 백업 |
| `--backup-only` | 백업만 |
| `--backup-eml` | 개별 `.eml` 도 저장 |
| `--workers N` | 병렬 (기본 1, 권장 1~5) |
| `--sleep S` | 요청 간격 (기본 0.2초) |
| `--hard` | 영구 삭제 (vs 휴지통) |
| `--dry-run` | 미리보기 (기본) |
| `--execute` | 실제 실행 |

## 백업 결과

`backups/` 폴더:
- `backup_{folder}_{YYYYMMDD_HHMMSS}.json` — 모든 메타
- `backup_{folder}_{YYYYMMDD_HHMMSS}.txt` — 사람 읽기 좋게
  ```
  [2024-06-15 14:30:00] sender@x.com → me@company.com | 회의 안내 📎
    안녕하세요. 다음 주 회의 일정 공유드립니다...
  ```
- (`--backup-eml` 시) `eml_{folder}_{YYYYMMDD_HHMMSS}/` 폴더에 개별 `.eml`

## 안전장치

1. **dry-run 기본** — `--execute` 명시 안 하면 조회+미리보기만
2. **delegated 권한** — 본인 메일함만 (Application 권한 X)
3. **429 자동 백오프** — `Retry-After` 따라 (최대 5회)
4. **로그** — `logs/deleted_YYYYMMDD_HHMMSS.jsonl` (msg_id, subject_head, from, received_at, result)
5. **휴지통 우선** — `--hard` 명시해야 영구 삭제
6. **토큰 캐시 권한** — `.token_cache.bin` 자동 600

## 자주 발생하는 에러

| 에러 | 해결 |
|---|---|
| `device flow 실패` | Authentication → Allow public client flows = Yes |
| `403 Forbidden` 조회 시 | Mail.ReadWrite scope 누락 또는 admin consent 필요 |
| `400 InvalidArgument $filter` | `--from` 이메일 오타 또는 폴더에 없는 필드 |
| `429 ratelimited` | 자동 백오프 — 그대로 두면 재시도 |
| `404 not_found` | 이미 삭제된 메시지 — skip |
| `--hard` 후 Recoverable Items 잔존 | Litigation Hold 정책 — admin 만 정리 가능 |

## 흐름

```
1. .env 로드 → MS365_CLIENT_ID + TENANT
2. MSAL Device Code Flow → access_token (캐시 재사용)
3. GET /me → 로그인 확인
4. GET /me/mailFolders/{folder}/messages?$filter=...&$orderby=receivedDateTime desc
   ├ nextLink 페이지네이션
   └ 429 시 Retry-After 백오프
5. 클라이언트 필터 (subject_contains / body_contains / keep_pattern)
6. 미리보기 (최근 5)
7. (옵션) 백업 — JSON + TXT [+ .eml]
8. dry-run: 종료 / execute:
   ├ --hard: DELETE /me/messages/{id}
   └ 기본:   POST /me/messages/{id}/move {destinationId: deleteditems}
9. 로그 (deleted_*.jsonl) + 카운트 집계
```

## 토큰 폐기 (작업 완료 후)
1. `.token_cache.bin` 삭제
2. Azure 포털 → App registrations → 본 App → **Delete**
