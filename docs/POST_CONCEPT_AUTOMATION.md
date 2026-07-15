# Post-Concept Automation — 승인 게이트와 자동화 구간

이 문서는 `scripts/run_post_concept.py`가 자동화하는 구간과, 그 앞뒤에 있는
사람 승인 게이트(Gate A/B/C)를 정의합니다.

## 승인 게이트

파이프라인에서 사람이 개입하는 지점은 세 곳뿐입니다.

| 게이트 | 이름 | 사람이 하는 일 | 통과 조건 |
| --- | --- | --- | --- |
| **Gate A** | 기획 승인 | `episode.json`(장면/대사/캐릭터 바이블/음성 프로필)과 대표 콘셉트 이미지를 확정 | `review/representative-image.png` 존재 + `music/composition-plan.json` 작성 |
| **Gate B** | 콘택트시트 검수 | 생성된 컷 이미지를 원본 해상도로 검수, 불량 컷(소품 개수·의상 위치·계절 연속성 등)을 골라냄 | 불량 컷 이미지를 지우고 재실행 → 전 컷 합격 |
| **Gate C** | 업로드 결정 | `ready_for_review` 상태의 최종 MP4를 확인하고 업로드 여부·메타데이터를 결정 | `upload_youtube_short.py --upload` 수동 실행 |

Gate A와 Gate C 사이의 모든 제작 공정은 자동입니다. Gate B는 이 자동 구간
안에 있는 반복 검수 루프로, 불량 컷만 지우고 같은 명령을 다시 실행하면
합격한 컷은 그대로 재사용되고 지운 컷만 다시 생성됩니다.

## 자동화 구간 (Gate B → Gate C span)

`run_post_concept.py --episode <episode.json> --reference <대표 이미지>` 한
번이 아래 다섯 단계를 순서대로 실행합니다. 보통 `background_job.py`로
백그라운드에서 돌립니다 (README의 사용 흐름 3단계 참고).

1. **이미지 프롬프트 생성** — `make_episode.py --write-prompts-only`
2. **컷 이미지 생성** — `generate_images_gemini.py` (대표 이미지를 스타일
   레퍼런스로 사용, 재시도/검증 포함, 이미 있는 컷은 건너뜀)
3. **음성/효과음 렌더** — `make_episode.py` 전체 렌더 (ElevenLabs TTS + SFX,
   컷당 음성 길이에 맞춘 1080x1920 세그먼트 합성 → `<id>-voice-sfx.mp4`)
4. **음악 플랜 길이 보정** — `music/composition-plan.json`의 chunk 길이를
   실제 렌더 길이에 맞게 스케일한 사본을
   `build-final/composition-plan.scaled.json`으로 저장 (원본 플랜은 수정하지
   않음)
5. **음악 생성 + 최종 믹스** — `finalize_episode.py --music-plan <scaled>`
   (ElevenLabs Music v2 → loudnorm 음량 평탄화 → 최종 MP4), 완료 시
   `automation/status.json`의 `stage`가 `ready_for_review`가 됨

## 재실행과 캐시

같은 명령을 다시 실행하면 비싼 산출물은 재사용되고, 입력이 바뀐 것만 다시
만들어집니다.

- **컷 이미지**: 파일이 있으면 건너뜀 — Gate B에서 불량 컷을 **지우는** 것이
  재생성 신호입니다.
- **TTS·효과음 (ElevenLabs)**: 출력 디렉토리의 `cache-manifest.json`에 컷별
  입력 해시(대사, 화자 프로필, 모델, 포맷 등)가 기록됩니다. `episode.json`의
  대사나 음성 설정을 고치면 해시가 달라져 **해당 컷만 자동 재생성**됩니다 —
  파일을 지울 필요가 없습니다.
- **세그먼트 렌더·최종 믹스**: 매 실행 다시 만듭니다 (ffmpeg 로컬 연산이라
  저렴).

## 상태 추적

- `automation/status.json` — `finalize_episode.py`가 갱신하는 단계 상태
  (`finalize_running` → `ready_for_review`), 최종 산출물 경로와 길이/해상도 포함
- `automation/*.log`, `*-job.json` — `background_job.py`가 남기는 실행 로그와
  PID/상태 파일
