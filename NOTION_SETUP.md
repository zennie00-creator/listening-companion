# Notion + Streamlit 첫 배포

GitHub Merge는 코드 반영입니다. Streamlit의 첫 앱 생성과 Secrets 등록은 따로 진행합니다. 기존 일지 에이전트 앱의 설정은 변경하지 않습니다.

## 1. Notion 전용 연결과 페이지

1. Notion 개발자 도구에서 **연결 만들기 → 신규 연결**을 누릅니다.
2. 이름은 **Listening Companion**, 인증 방법은 **API 토큰**으로 만듭니다. 콘텐츠 읽기·추가·수정 권한을 허용합니다.
3. 감상실용 빈 페이지 **Listening Companion**을 준비합니다.
4. 그 페이지 오른쪽 위 **••• → 연결 → 연결 추가**(화면에 따라 **더 보기** 안에 있음)에서 전용 연결을 선택합니다. 개발자 도구의 연결 상세 화면에 **Content access / 콘텐츠 액세스**가 있다면 거기서 이 페이지를 선택해도 됩니다.
5. 토큰은 Streamlit Secrets에만 등록합니다. 채팅이나 GitHub에 보내지 않습니다. 노션 페이지는 웹에 공개하지 않습니다.

## 2. 새 Streamlit 앱 만들기

1. `https://share.streamlit.io`의 오른쪽 위 **Create app / Create**를 누릅니다.
2. 이미 코드가 있는 앱, 또는 GitHub에서 배포하는 항목을 선택합니다.
3. 다음을 지정합니다. 목록에 없으면 직접 입력할 수 있습니다.

| 항목 | 값 |
| --- | --- |
| Repository | `zennie00-creator/listening-companion` |
| Branch | `main` |
| Main file path | `streamlit_app.py` |
| Python | `3.12` |

4. **Advanced settings → Secrets**를 엽니다. 아래 예시에서 빈칸에 실제 값을 입력합니다.

```toml
LC_DEPLOYMENT_MODE = "cloud"
STORAGE_BACKEND = "notion"
NOTION_TOKEN = ""
NOTION_PAGE_ID = ""
APP_PASSWORD = ""
OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4.1-mini"
```

- `NOTION_TOKEN`: 방금 만든 Listening Companion 전용 연결의 토큰.
- `NOTION_PAGE_ID`: 전용 페이지 주소 끝의 32자리 ID. 페이지 전체 주소도 입력 가능.
- `APP_PASSWORD`: 새 앱을 여는 나만의 비밀번호. 12자 이상이며 다른 서비스와 겹치지 않는 긴 비밀번호를 사용합니다. GitHub에는 저장하지 않습니다.
- `OPENAI_API_KEY`: 기존 **Listening Companion**의 키를 재사용합니다. 별도 일지 에이전트의 키를 복사하지 않습니다. 이 값이 비어 있어도 감상 저장·음반 직접 연결·백업은 사용할 수 있고 AI 기능만 사용할 수 없습니다.
- 비밀번호를 해시로 관리하려면 `APP_PASSWORD` 대신 `scripts/password_hash.py`의 결과를 `APP_PASSWORD_HASH`에 넣습니다.
- Notion을 사용하면 `DATABASE_URL`은 필요 없습니다.

5. **Save → Deploy**를 누릅니다. 처음 배포한 뒤에야 My apps 목록에 새 앱이 표시됩니다.
6. 배포된 앱에 비밀번호로 접속합니다. 이후 Secrets 수정은 My apps의 해당 앱 **⋮ → Settings → Secrets**에서 합니다.

## 3. 기존 기록 복원

앱 **백업·가져오기**에서 개인 백업 ZIP을 선택하고 건수를 확인한 뒤 가져옵니다. 사진·수정 이력·청취 환경·실험을 확인하고, 새 감상 하나를 저장한 후 앱을 다시 열어 확인합니다. 검증 전에는 기존 Sites를 종료하지 않습니다.

## Notion 저장 방식과 한계

- 전용 페이지 아래에 저장할 때마다 새로운 보관 페이지가 생깁니다. 각 저장본에는 원문·사진·수정 이력·환경·실험이 들어 있는 전체 백업과 검증 정보가 포함됩니다.
- Notion은 앱의 영구 보관함입니다. 감상 작성·검색·수정은 Streamlit에서 합니다. Notion 표의 행을 직접 편집해서 앱 기록을 바꾸는 방식은 지원하지 않습니다.
- 긴 원문과 원본 사진을 잘라내지 않습니다. 4MB 단위로 나눈 유효한 ZIP 파일로 보관하여 파일당 5MB 제한에 맞춥니다. 한 저장본의 전체 크기는 최대 80MB입니다.
- 저장 직후 원격 파일을 다시 받아 검증한 뒤 성공으로 표시합니다. 업로드 실패 시 로컬에만 저장하고 성공한 것처럼 표시하지 않습니다. 서버의 임시 작업 파일은 작업 후 삭제합니다.
- 저장본이 쌓일수록 업로드 시간과 Notion 저장 용량이 증가합니다. 자동 삭제는 하지 않습니다. 보관 페이지의 이름·검증 정보·첨부 파일을 직접 수정하거나 삭제하지 마세요.
- **한 사람·하나의 Streamlit 배포**를 위한 방식입니다. Notion에는 SQL과 같은 트랜잭션이나 원자적 비교 후 갱신이 없습니다. 서로 다른 서버가 같은 기록에서 동시에 저장하면 두 백업을 보존하고 자동 읽기·덮어쓰기를 중단합니다. 이 경우 각 보관 페이지의 첨부 파일을 확보한 뒤 수동 병합·복구가 필요합니다. 여러 서버에서 안전한 동시 편집이 필요하면 PostgreSQL을 사용합니다.
- Notion 방식의 로그인 실패 제한은 한 앱 프로세스의 세션 사이에 공유되지만 서버 재시작 때 초기화됩니다. 개인 앱 비밀번호와 Streamlit 공유 설정을 함께 사용합니다. PostgreSQL 방식은 실패 제한도 DB에 보존합니다.
- 자동 검사는 모의 Notion API로 실패·충돌·복원 경로를 검사합니다. 실제 토큰과 Streamlit 앱을 연결한 뒤 운영 환경의 저장·재접속 검증이 필요합니다.

공식 안내:

- [Streamlit 첫 배포와 Advanced settings](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Streamlit Secrets 관리](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
- [Notion 내부 연결과 페이지 접근 권한](https://developers.notion.com/guides/get-started/internal-connections)
- [Notion 파일 업로드](https://developers.notion.com/guides/data-apis/uploading-small-files)
