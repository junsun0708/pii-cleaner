# MS365 메일 — 1통 동작 확인 PoC

본격 MVP (Slack 도구와 동등한 백업/필터/병렬) 진행 전, **1통만이라도 fetch + 휴지통 이동이 되는지** 확인용 단계.

## 0. 회사 정책 사전 확인

| 항목 | 확인 |
|---|---|
| Litigation Hold / In-Place Hold | ❌ 적용되면 영구 삭제 차단 (IT 담당자 문의) |
| Retention Policy | ❌ 회계/계약 메일 5~7년 자동 보존 |
| eDiscovery 진행 중 | ❌ 모든 삭제 차단 |
| App 등록 차단 (Tenant 정책) | ⚠ Azure 「App registrations」 접근 가능 여부 |

→ 위 3개 중 하나라도 적용이면 PoC 중단 권장.

## 1. Azure AD App 등록

1. https://portal.azure.com → **Azure Active Directory** → **App registrations** → **New registration**
2. 설정:
   - Name: `pii-cleaner-ms365` (자유)
   - Supported account types: **「Accounts in this organizational directory only (single tenant)」**
   - Redirect URI: (비워둠 — Public client + Device Code 흐름)
3. **Register** 클릭
4. Overview 페이지에서:
   - **Application (client) ID** 복사 → `.env` 의 `MS365_CLIENT_ID`
   - **Directory (tenant) ID** 복사 → `.env` 의 `MS365_TENANT_ID`

## 2. 권한 (Scope) 추가

좌측 메뉴 **「API permissions」** → **「Add a permission」** → **「Microsoft Graph」** → **「Delegated permissions」** :

| Scope | 용도 |
|---|---|
| `Mail.ReadWrite` | 본인 메일 조회 + 삭제 |
| `User.Read` | 로그인 사용자 정보 확인 |
| `offline_access` | refresh token (자동 추가됨) |

추가 후 **「Grant admin consent」** 버튼이 필요할 수 있음 (회사 정책에 따라 — 회색 처리 시 관리자에게 요청).

## 3. Public Client Flow 허용

좌측 **「Authentication」** → 하단 **「Advanced settings」** → **「Allow public client flows」** → **Yes** → **Save**

(Device Code Flow 동작에 필수)

## 4. 로컬 환경 세팅

```bash
cd ~/a-projects/pii-cleaner/poc_ms365

cp .env.example .env
nano .env   # MS365_CLIENT_ID + MS365_TENANT_ID 채움

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 5. PoC 실행 — 3단계

### 5-1. 조회만 (안전)
```bash
python poc_one_message.py
```

출력 예시:
```
─────────────────────────────────────────
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code XXXX-XXXX
─────────────────────────────────────────
로그인: 정용후 <user@회사.com>

=== 최신 메일 1통 조회 (폴더=inbox) ===
  id          : AAMkAGI...
  receivedAt  : 2026-05-21T08:30:00Z
  from        : noreply@github.com
  subject     : [pii-cleaner] PR opened
  attachments : False
  preview     : @user opened pull request #...

(다음 단계: --delete 휴지통 이동 / --hard 영구 삭제)
```

→ ✅ 여기까지 되면 **권한 + 인증 OK**.

### 5-2. 휴지통 이동 (복구 가능)
```bash
python poc_one_message.py --delete
```

Outlook 의 「삭제된 항목」 폴더로 메일이 옮겨짐. 거기서 **「폴더로 이동」 우클릭으로 복구 가능**.

### 5-3. 영구 삭제 (회복 불가 — 정책 검증)
```bash
python poc_one_message.py --folder deleteditems --hard
```

- ✅ 회복 불가 = MVP 진행 가능
- ❌ 200 OK 받았지만 Recoverable Items 에 잔존 = Litigation Hold 추정. 다른 도구 (Outlook GUI) 도 동일하게 잔존하므로 본 도구의 한계 X

## 6. 폴더 옵션
- `--folder inbox` (기본) — 받은편지함
- `--folder sentitems` — 보낸편지함
- `--folder drafts` — 임시 보관함
- `--folder deleteditems` — 삭제된 항목 (재삭제)

## 7. 결과 보고 후 MVP 진행
5-2 또는 5-3 까지 동작 확인되면 본격 MVP 시작:

```
mvp_ms365_self_delete/
├ ms365_self_delete.py   ← Slack 도구와 동등 (백업/필터/병렬/dry-run)
├ .env.example
├ requirements.txt
└ README.md
```

기능:
- `--since / --until` 기간 필터
- `--from / --subject-contains / --keep-pattern` 필터
- `--backup` (.eml + JSON + TXT) 전체 백업
- `--workers N` 병렬 처리
- `--hard` 영구 삭제 (Recoverable Items 정책 영향)
- `--dry-run` (기본)

## 토큰 캐시
- `.token_cache.bin` (gitignored) — refresh token 보관, 다음 실행 시 자동 로그인
- 권한 폐기 시 파일 삭제 + Azure App 삭제
