# Fairy Tale Shorts Pipeline

고전 동화를 현대적 관점으로 재구성한 유튜브 쇼츠를, 콘셉트 승인 이후 **완전 자동으로** 만들어내는 파이프라인입니다.

An end-to-end automation pipeline that turns reimagined classic fairy tales into vertical YouTube Shorts — AI images, per-character voices, sound effects, scene-aware background music, rendering, and upload.

▶️ **실제 운영 채널: [바른 생활 대안동화 Fairy Tale Lab](https://www.youtube.com/channel/UCWVR2l7N0P4Uo0mml7Cu2wA)** — 이 파이프라인으로 제작된 에피소드들을 볼 수 있습니다.

## 무엇을 자동화하나

에피소드 정의 파일(`episode.json`) 하나와 승인된 대표 콘셉트 이미지 1장을 입력하면:

```text
episode.json + 대표 이미지
  → 대사별 이미지 생성          (Gemini 이미지 모델, 대표 이미지를 레퍼런스로 캐릭터 일관성 유지)
  → 캐릭터별 한국어 음성        (ElevenLabs TTS, 대사별 화자 프로필)
  → 핵심 컷 효과음              (ElevenLabs Sound Effects)
  → 1080x1920 기본 렌더         (ffmpeg, 컷당 음성 길이에 맞춘 세그먼트 합성)
  → 장면 흐름 배경음악          (ElevenLabs Music, 씬별 composition plan + 실제 렌더 길이 자동 보정)
  → 고정 음량 최종 믹스         (loudnorm, 에피소드별 다이내믹 레인지 설정 가능)
  → (수동 승인 후) 유튜브 업로드
```

편당 순수 생성 비용은 약 $1~3 수준이며(이미지 15~30컷 기준), 사람의 개입은 기획 승인과 콘택트시트 검수, 업로드 결정뿐입니다.

## 구성

| 스크립트 | 역할 |
| --- | --- |
| `scripts/new_episode.py` | 템플릿에서 새 에피소드 폴더 생성 |
| `scripts/make_episode.py` | 이미지 프롬프트 생성, TTS/효과음 생성, 세그먼트 렌더 |
| `scripts/generate_images_gemini.py` | 대사별 이미지 생성 (레퍼런스 이미지 기반, 재시도/검증 포함) |
| `scripts/run_post_concept.py` | 위 전체를 잇는 오케스트레이터 (이미지→렌더→음악 길이 보정→최종 믹스) |
| `scripts/finalize_episode.py` | Music 생성, 음량 평탄화, 최종 MP4 믹스 |
| `scripts/background_job.py` | 긴 작업을 PID/로그/상태 파일과 함께 백그라운드 실행 |
| `scripts/upload_youtube_short.py` | OAuth 인증과 유튜브 업로드 |
| `scripts/record_metrics.py` | 채널/영상 지표를 JSONL로 주기 기록 (Data API + Analytics API) |

승인 게이트(Gate A/B/C)와 자동화 구간·캐시 동작의 상세는 [docs/POST_CONCEPT_AUTOMATION.md](docs/POST_CONCEPT_AUTOMATION.md)를 참고하세요.

## 준비물

1. Python 3.12+, ffmpeg
2. `.env` 파일 (`.env.example` 참고):
   - `OPENAI_API_KEY` — (선택) OpenAI TTS/이미지 사용 시
   - `ELEVENLABS_API_KEY` — 음성, 효과음, 음악
   - `GEMINI_API_KEY` — 대사별 이미지 생성
3. 유튜브 업로드용 Google OAuth 클라이언트: `client_secret.json`을 리포 루트에 두고 `upload_youtube_short.py --auth-url`로 최초 1회 인증

## 사용 흐름

```bash
# 1. 새 에피소드 스캐폴드
.venv/bin/python scripts/new_episode.py --id 012-my-story --title "나의 이야기"

# 2. episode.json 작성 (장면/대사/비주얼/캐릭터 바이블/효과음/음성 프로필)
#    + 대표 콘셉트 이미지를 review/representative-image.png 로 승인
#    + music/composition-plan.json 작성 (ElevenLabs Music composition plan)

# 3. 전체 파이프라인 실행 (백그라운드)
.venv/bin/python scripts/background_job.py start \
  --name "012 full auto" --cwd . \
  --log episodes/012-my-story/automation/full-auto.log \
  --pid episodes/012-my-story/automation/full-auto.pid \
  --status episodes/012-my-story/automation/full-auto-job.json \
  -- \
  .venv/bin/python scripts/run_post_concept.py \
    --episode episodes/012-my-story/episode.json \
    --reference episodes/012-my-story/review/representative-image.png

# 4. 콘택트시트 검수 후 잘못 나온 컷만 지우고 재실행 (캐시된 컷은 건너뜀)

# 5. 업로드 (수동 결정)
.venv/bin/python scripts/upload_youtube_short.py --upload \
  --file episodes/012-my-story/build-final/012-my-story-elevenlabs-music-v2-steady.mp4 \
  --privacy public --title "제목 #Shorts" --description "..." --tags "..."
```

## 배운 것들 (요약)

- 이미지 생성은 대표 이미지를 매 요청의 레퍼런스로 넣으면 캐릭터 일관성이 크게 좋아지지만, 레퍼런스의 의상·계절이 무관한 컷에 새어 들어가는 부작용이 있어 컷별 시각 지시문에 "이 컷에 없어야 할 것"을 명시해야 합니다.
- AI 이미지의 고질 결함 유형: 소품 개수(활 2개), 소품 색 변화, 의상 착용 위치(목도리를 발목에), 계절·장소 연속성, 읽을 수 있는 텍스트. 콘택트시트는 원본 해상도로 검수해야 합니다.
- 음악 생성 모델은 다이내믹 대비(조용한 구간→피날레 폭발) 지시를 잘 따르지 않아, 구간 분리·덕킹·모티프 리프라이즈 같은 ffmpeg 후처리가 더 확실합니다. 고정 음량 파이프라인(loudnorm)의 LRA도 에피소드별로 조절할 수 있게 했습니다.

## 라이선스

[MIT](LICENSE)
