# Stealth Copilot

Stealth Copilot is a real-time interview assistant that listens to questions and shows concise, readable answers in a discreet, always-on-top window. It uses AssemblyAI for live transcription and OpenAI for answer generation.

## Features

- Real-time transcription from system audio (via VB-Cable)
- Fast, clean answers optimized for on-screen reading
- Stealth overlay window with hotkey controls
- Context support: resume/JD upload, company name, interview stage
- Rolling conversation memory for continuity

## Requirements

- Windows 10/11
- Python 3.10+
- [VB-CABLE Virtual Audio Device](https://vb-audio.com/Cable/)

## Language and Dependencies

- Language: Python
- Dependencies (installed via `requirements.txt`):
  - pynput
  - pyaudio
  - websocket-client
  - requests
  - customtkinter
  - packaging
  - pywin32
  - PyPDF2
  - python-docx
  - openai

## Installation

1) Clone the repo:

```bash
git clone https://github.com/hvredde-stack/MeetingopenAI.git
cd MeetingopenAI
```

2) Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Create `config.py` in the project root with your API keys:

```python
# config.py
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"
ASSEMBLYAI_API_KEY = "YOUR_ASSEMBLYAI_API_KEY"
```

## Usage

1) Route meeting audio to VB-Cable:
- Set your meeting app output device to **CABLE Input (VB-Audio Virtual Cable)**.
- The app listens on **CABLE Output**.

2) Run the app:

```bash
python stealth_copilot.py
```

3) Hotkeys:
- `Ctrl+Alt+H`: show/hide window
- `Ctrl+Alt+U`: upload resume/JD/context file (PDF/DOCX/TXT)
- `Ctrl+Alt+C`: set company name
- `Ctrl+Alt+S`: set interview stage


## Run without PowerShell

Start the app without keeping a terminal open:

```powershell
Start-Process pythonw -ArgumentList "stealth_copilot.py"
```

Stop it:

```powershell
taskkill /IM pythonw.exe /F
```

## Tips

- If the app responds too early or too late, adjust the end-of-turn threshold in `stealth_copilot.py`.
- Keep answers short by editing the system prompt block in `stealth_copilot.py`.
- The overlay keeps the last 1000 lines for scrollback.

## Troubleshooting

- If you get no audio, confirm VB-Cable is installed and your meeting app output is set correctly.
- If the overlay is hidden from screen capture, that is expected in stealth mode.
