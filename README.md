# Stealth Copilot

Stealth Copilot is a real-time interview assistant that listens to questions and provides concise, bullet-point answers on a discreet, always-on-top window. It uses AssemblyAI for real-time transcription and Groq with the Llama 3 model for generating answers.

## Features

- **Real-time Transcription**: Captures audio from your system (e.g., a virtual meeting) and transcribes it in real-time.
- **AI-Powered Answers**: Sends the transcribed question to the Groq API to get a concise, expert answer.
- **Discreet Display**: Shows the answer in a simple, borderless, always-on-top window that you can move around your screen.
- **Hotkey Control**: Toggle the visibility of the answer window with a hotkey (`Ctrl+Alt+H`).
- **Virtual Audio Input**: Designed to listen to a virtual audio device like VB-Cable, so it doesn't capture your own microphone.

## Getting Started

### Prerequisites

- Python 3.x
- [VB-CABLE Virtual Audio Device](https://vb-audio.com/Cable/)

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/hvredde-stack/Meeting_AI.git
    cd Meeting_AI
    ```

2.  **Install the required Python packages:**
    ```bash
    pip install -r requirements.txt
    ```

### Configuration

1.  **Create a `config.py` file** in the same directory as `stealth_copilot.py`.

2.  **Add your API keys** to the `config.py` file. You will need keys from Groq and AssemblyAI.
    ```python
    # config.py
    GROQ_API_KEY = "YOUR_GROQ_API_KEY"
    ASSEMBLYAI_API_KEY = "YOUR_ASSEMBLYAI_API_KEY"
    ```

## Usage

1.  **Configure your system's audio:**
    - Set the audio output of the application you want to listen to (e.g., your browser, Microsoft Teams, Zoom) to **CABLE Input (VB-Audio Virtual Cable)**. This routes the meeting audio into the virtual cable.
    - The script will automatically listen to the other end of the cable, **CABLE Output**.

2.  **Run the script:**
    ```bash
    python stealth_copilot.py
    ```

3.  **Using the Copilot:**
    - When the script is running, a black window will appear. This is where the answers will be displayed.
    - When a question is spoken in the audio routed through VB-Cable, the transcribed text will appear in your console, and the AI-generated answer will appear in the black window.
    - You can drag the window by clicking and dragging it.
    - To hide or show the window, use the hotkey: `Ctrl+Alt+H`.
