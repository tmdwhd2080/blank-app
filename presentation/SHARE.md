# ETF Alpha — 소개자료 & 데모 공유 가이드

이 폴더와 `docs/` 에는 제품 소개에 필요한 산출물이 들어 있습니다.

| 산출물 | 경로 | 용도 |
|---|---|---|
| **소개 PPT** | `presentation/ETF_Alpha_소개자료.pptx` | 구조·수익구조 13장 발표자료 (PowerPoint) |
| **라이브 데모(단일 파일)** | `docs/index.html` | 백엔드 없이 열리는 제품 데모. **링크 공유용** |
| PPT 생성 스크립트 | `presentation/build_deck.py` | 내용 수정 후 PPT 재생성 |
| 데모 생성 스크립트 | `presentation/build_demo.py` | `dashboard.html` 기반 데모 재생성 |

재생성:
```bash
python presentation/build_deck.py    # → presentation/ETF_Alpha_소개자료.pptx
python presentation/build_demo.py    # → docs/index.html
```

---

## 데모를 "링크"로 공유하는 3가지 방법

`docs/index.html` 은 **백엔드 없이 단독 실행**되는 정적 파일이라(샘플 데이터 내장),
어떤 정적 호스팅에 올려도 바로 동작합니다.

### 방법 A. GitHub Pages (무료·영구, 추천)

이 저장소(`tmdwhd2080/blank-app`)는 이미 GitHub 에 연결돼 있습니다.

1. 커밋 & 푸시
   ```bash
   git add docs presentation
   git commit -m "Add ETF Alpha pitch deck and shareable demo"
   git push origin main
   ```
2. GitHub 저장소 → **Settings → Pages**
   - **Source:** `Deploy from a branch`
   - **Branch:** `main`  /  **Folder:** `/docs`  → **Save**
3. 1~2분 뒤 아래 주소가 공개됩니다 (이 링크를 공유):
   ```
   https://tmdwhd2080.github.io/blank-app/
   ```

> `docs/.nojekyll` 가 포함돼 있어 Jekyll 가공 없이 그대로 서빙됩니다.

### 방법 B. Netlify Drop (가입 없이 즉시)

1. https://app.netlify.com/drop 접속
2. `docs/` 폴더를 브라우저로 **드래그&드롭**
3. 즉시 `https://<랜덤이름>.netlify.app` 링크 생성 → 공유

### 방법 C. 실시간 백엔드까지 시연 (Cloudflare Tunnel / ngrok)

데모 데이터가 아니라 **실제 KIS·AI 파이프라인**을 외부에 보여주려면,
로컬 API 서버를 터널로 노출합니다.

```bash
# 1) 실제 추천 1회 생성
python -m news_crawl.run_pipeline --as-of 2026-06-13T09:10:00+09:00

# 2) 대시보드 API 서버 실행 (실데이터 버전: news_crawl/dashboard.html 사용)
python -m news_crawl.api_server --port 8765

# 3) 외부 공개 (택1)
cloudflared tunnel --url http://127.0.0.1:8765
#   또는
ngrok http 8765
```
터널이 출력하는 `https://....` 주소를 공유하면, 상대방이 **실시간 추천**을 봅니다.
(이 방식은 내 PC가 켜져 있는 동안만 유효 — 상시 공개는 방법 A 권장)

---

## PPT 를 링크로 공유하려면

- **OneDrive / Google Drive** 업로드 후 "링크가 있는 모든 사용자" 공유
- Google Slides 로 변환하면 브라우저에서 바로 열람 가능
- 또는 PowerPoint 에서 `파일 → 내보내기 → PDF` 후 PDF 링크 공유

---

## 공유 시 유의 (면책)

데모/PPT 의 추천·점수·수익 시나리오는 **설명용 예시**이며 투자 자문이나 매매 권유가 아닙니다.
수익 구조의 수치는 가정에 기반한 추정치입니다.
