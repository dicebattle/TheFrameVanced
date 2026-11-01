# TheFrameVanced

삼성 더 프레임 TV를 절기, 날씨, 시간이 느껴지는 디지털 액자로 바꿉니다.

Turns a Samsung The Frame TV into a living digital frame: it picks images that match the local season, weather and time of day, prints a small caption and shows them in Art Mode.

## 무엇을 하나요

- 10분마다 지금 시간대와 날씨를 확인합니다.
- 시간대나 날씨가 바뀌거나 정해 둔 간격(기본 3시간)이 지나면 어울리는 이미지로 바꿉니다.
- 이미지에 절기, 날씨, 기온, 작품명을 작게 넣습니다.
- 명화는 자르지 않고 어두운 벽에 건 모습으로 보여 줍니다.
- 관리 페이지에서 글자 위치와 모양, 이미지, TV 연결, 지역, 교체 주기를 설정합니다.
- TV를 시청 중이면 화면을 건드리지 않고, TV가 꺼져 있으면 깨우지 않습니다.

## 준비물

- 아트모드를 지원하는 더 프레임 TV (2021년형 LS03A, Art API 2.03에서만 시험했습니다)
- TV와 같은 네트워크에 있는 Linux 서버
- Python 3.10 이상, `systemd`, Noto Sans CJK 글꼴 (`fonts-noto-cjk`)

글꼴 경로는 Ubuntu 기준으로 `frame.py`의 `FONT_PATHS`에 들어 있습니다. 다른 배포판이면 이 값을 바꾸세요.

## 설치

```bash
git clone https://github.com/dicebattle/TheFrameVanced.git ~/TheFrameVanced
cd ~/TheFrameVanced
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p data/library
cp config.example.json data/config.json
```

이미지를 내려받습니다. 위키미디어 요청 제한을 지키느라 시간이 걸릴 수 있습니다. 다시 실행하면 이어서 받습니다.

```bash
.venv/bin/python tools/build_library.py library/manifest.json data/library
```

서비스를 등록합니다.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now theframevanced-admin.service
loginctl enable-linger "$USER"
```

## 관리 페이지

관리 페이지에는 로그인이 없어서 서버의 `127.0.0.1:8765`에서만 열립니다. 다른 컴퓨터에서는 SSH 터널로 여세요.

```bash
ssh -N -L 8765:127.0.0.1:8765 사용자@서버주소
```

그다음 http://127.0.0.1:8765 를 엽니다.

1. `TV` 탭에서 `네트워크에서 찾기`로 TV를 고릅니다.
2. `자동 교체` 탭에서 지역 이름을 찾습니다. 좌표와 시간대는 자동으로 맞춰집니다.
3. `글자` 탭에서 글자를 끌어 위치를 정합니다.
4. `자동 교체` 탭에서 `자동 교체 켜기`를 켭니다.

## 이전 이미지 자동 삭제

글자를 넣으려면 교체할 때마다 새 이미지를 TV에 올려야 해서, 그대로 두면 TV 저장 공간이 찹니다. `자동 교체` 탭의 `이전 이미지 자동 삭제`를 켜면 새 이미지 표시를 확인한 뒤 이전 이미지를 지웁니다.

- TV에는 휴지통이 없어 영구 삭제됩니다.
- 이 프로그램이 올린 이미지만 지웁니다. 직접 올린 사진과 기본 작품은 지우지 않습니다.
- 기본값은 꺼짐입니다.

## 외부로 보내는 정보

| 서비스 | 보내는 내용 | 시점 |
|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | 설정한 지역의 좌표 | 10분마다, 지역을 정할 때 |
| [OpenStreetMap Nominatim](https://nominatim.org/) | 입력한 지역 이름 | `찾기`를 누를 때만 |
| Wikimedia Commons | 이미지 요청 | 이미지를 내려받을 때 |

## 이미지와 저작권

이 저장소에는 이미지 파일이 없고, 출처 목록 `library/manifest.json`만 있습니다. 각 항목에 원본 주소, 작가, 라이선스가 들어 있습니다. 라이선스는 이미지마다 다르니, 공개하거나 재배포할 때는 해당 조건을 지키세요. CC BY-SA 사진은 작가 표시가 필요합니다.

코드 라이선스는 MIT입니다. TV 연결에는 LGPL-3.0인 [samsung-tv-ws-api](https://github.com/xchwarze/samsung-tv-ws-api)를 사용합니다.

## 알려진 한계

- `set_artmode()`는 이 TV에서 응답 없이 멈출 수 있어 사용하지 않습니다. 그래서 프로그램이 아트모드를 켜지는 않습니다.
- CLI를 쓸 때는 `--no-print-token`을 붙이세요. 없으면 시작할 때 리모컨 채널의 허용을 기다리다 실패합니다.
- 다른 연식의 TV에서는 시험하지 않았습니다.

## 테스트

```bash
.venv/bin/python -m unittest discover tests
```
