# 공장 한 창 (Gongjang Hanchang)

견적 → 주문 → 작업지시 → 출하까지 **한 화면 흐름**으로 잇는, 주문 생산 제조 소기업용 가벼운 MES.
회사 PC 한 대에 폴더를 복사하고 `실행.bat`만 누르면 돌아갑니다. 외부 패키지·클라우드 계정·월 서버비가 없습니다(Python 표준 라이브러리 + SQLite).

> 라이선스: **비상업 목적은 무료**(PolyForm Noncommercial 1.0.0). **상업적으로 쓰고 싶다면 함께 개발합시다** → [COMMERCIAL.md](COMMERCIAL.md)

## 무엇을 하나

| 화면 | 하는 일 |
|---|---|
| 오늘 | 납기 지연·3일 안 출하·미결제·7일 넘게 답 없는 견적·곧 마감 업무를 한눈에 |
| 견적 | 품목·수량만 넣으면 서버가 금액 계산(부가세 10%), 인쇄, 거래처 메일 발송 |
| 주문 | 견적을 주문으로 한 번에 변환(납기·배송지 필수, 사업자등록증 미확인 거래처 경고), 작업 파일 첨부 |
| 현장 | 공정 단계별 작업지시 보드. 큰 글씨·큰 버튼, 종이 작업지시서 없이 다음 단계로 |
| 캘린더 | 월별 출하 일정 |
| 부하 | 일별 작업시간 합계와 하루 가능 시간 비교, 주문별 일정 막대 |
| 업무 | 기한순 할 일, 팀 공유, 분류 색 |
| 개선요청 | 현장 의견을 모으고 상태(접수·진행·완료) 관리 |
| 설정 | 공정 단계·업종 프리셋·직원 계정(권한 3종)·메일 서버·백업 |

업종 프리셋(일반 제조·금속 가공·인쇄/패키지·식품)은 `presets/*.json` 한 파일입니다. 새 업종은 코드 수정 없이 JSON만 추가하면 됩니다.

## 빠른 시작

1. Python 3.10 이상 설치 (Windows는 설치 시 *Add to PATH* 체크)
2. 이 저장소를 내려받아 폴더째 서버 PC에 둔다
3. **Windows**: `실행.bat` 더블클릭 / **macOS·Linux**: `python3 server.py`
4. 브라우저가 열리면 처음 설정(회사명·업종·관리자 이름과 PIN)
5. 다른 PC·태블릿·폰은 같은 와이파이에서 `http://서버PC의IP:8780` 접속

서버는 기본으로 `0.0.0.0:8780`에서 듣습니다. 이 PC에서만 쓰려면 `MES_HOST=127.0.0.1`, 포트는 `python3 server.py 9000` 처럼 바꿉니다. 데이터 폴더는 `MES_DATA` 환경변수로 바꿉니다.

## 권한

| 권한 | 할 수 있는 일 |
|---|---|
| 관리자 | 전부 + 사용자·설정·백업 |
| 사무 | 견적·주문·거래처·품목·업무 |
| 현장 | 주문·작업지시 보기, 공정 단계 변경, 파일 받기, 업무·개선요청 |

## 데이터와 백업

- 모든 데이터는 `data/` 폴더(`mes.db`, `files/`, `config.json`)에 있습니다. 폴더째 복사하면 이전됩니다.
- 서버가 하루 한 번 `data/backup/`에 자동 백업합니다(14개 보관). 이 폴더를 USB·NAS에도 복사해 두세요.
- `data/config.json`에는 메일 비밀번호가 **평문**으로 들어갑니다. 폴더 접근 권한이 곧 보안 경계입니다.

## 보안 주의

- **사내망 전용**으로 쓰세요. 인터넷에 직접 열지 마세요. 외부 접속은 VPN을 쓰세요.
- PIN은 PBKDF2로 저장되고, 세션 쿠키는 HttpOnly·SameSite=Strict이며, 로그인 5회 실패 시 1분 잠깁니다. HTTPS는 지원하지 않습니다(사내망 전제).
- 취약점은 공개 이슈 대신 [SECURITY.md](SECURITY.md)의 방법으로 알려 주세요.

## 개발과 테스트

```bash
python3 test_mes.py        # 서버 테스트 (임시 폴더·가짜 데이터만 사용)
```

- 서버: `server.py` 한 파일 · 화면: `static/index.html` 한 파일. 일부러 작게 유지합니다.
- 합격 기준과 설계 근거: [docs/기획서.md](docs/기획서.md)
- 방향과 이슈 처리 기준: [docs/ROADMAP.md](docs/ROADMAP.md)
- 참여 방법: [CONTRIBUTING.md](CONTRIBUTING.md)

## 알려진 한계

- 서버를 재시작하면 모두 다시 로그인해야 합니다(세션이 메모리에만 있음).
- 백업 폴더가 같은 PC 안에 있어 PC 고장 시 함께 잃을 수 있습니다.
- 실제 SMTP 서버 발송, 여러 PC 동시 접속 부하, 실제 공장 현장 사용은 아직 검증하지 못했습니다. 시범 운영 제보를 환영합니다.
- 재고·BOM, 거래처·품목 CSV 가져오기는 아직 없습니다.

## 영감과 면책

오토프로그(AutoFrog)의 영상 「외주 없이 사장이 직접 만든 MES, 직원들은 어떻게 생각할까요?」(https://youtu.be/r86eHFMRlq4)에서 보인 업무 흐름(견적→주문→작업지시→배송 캘린더→부하 간트→업무관리)에서 영감을 받아 처음부터 새로 만들었습니다. 해당 회사의 코드·화면·상표와 무관하며 제휴·보증 관계가 없습니다.

## English summary

*Gongjang Hanchang* is a zero-dependency (Python stdlib + SQLite) lightweight MES for small make-to-order factories: quote → order → shop-floor work order → shipping, with a load chart, calendar and task board. Run `python3 server.py`, open `http://localhost:8780`. UI language is Korean. Free for **non-commercial** use under PolyForm Noncommercial 1.0.0; for commercial use, open a discussion to co-develop — see [COMMERCIAL.md](COMMERCIAL.md).
