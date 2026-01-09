import threading
import queue
import time
import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog
from pynput import keyboard
import pyaudio
import websocket
import json
import requests
import sys
import ctypes
import os
import PyPDF2
from docx import Document
from openai import OpenAI

# ========================
# Configuration & Constants
# ========================

import config
OPENAI_API_KEY = config.OPENAI_API_KEY
ASSEMBLYAI_API_KEY = config.ASSEMBLYAI_API_KEY

AI_MODEL = "gpt-5.1"  # Uses new Responses API
HOTKEY = '<ctrl>+<alt>+h'

# Initialize OpenAI Client
client = OpenAI(api_key=OPENAI_API_KEY)

# Audio Constants
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000

# UI Constants
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# ========================
# Global State
# ========================

# Queue now holds dicts: {"type": "text"|"clear"|"error", "content": ...}
answer_queue = queue.Queue()
current_interim = ""
ws = None
is_running = True

# Context Management
system_instruction_default = (
    "You are an experienced and highly skilled professional responding to an interview question. "
    "Your goal is to provide a direct, concise, and authoritative answer that demonstrates deep practical knowledge. "
    "Speak in the first person ('I would...', 'In my experience...'). Where appropriate, integrate specific "
    "technical commands, tools, or best-practice processes to showcase expertise. "
    "Focus on actionable insights and practical steps. Avoid lengthy theoretical explanations; "
    "aim for a succinct, knowledgeable answer that sounds confident and is easy to deliver verbally. "
    "Structure your response as a flowing, professional paragraph."
)

global_context = ""
custom_instructions = system_instruction_default

# ========================
# File Processing
# ========================

def extract_text_from_file(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    text = ""
    try:
        if ext == ".pdf":
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() + "\n"
        elif ext == ".docx":
            doc = Document(file_path)
            for para in doc.paragraphs:
                text += para.text + "\n"
        elif ext == ".txt":
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
        return text.strip()
    except Exception as e:
        print(f"[ERROR] Failed to read {file_path}: {e}")
        return ""

# ========================
# AI Interaction (Streaming)
# ========================

def generate_and_stream_response(transcript):
    print(f"[DEBUG] Sending to OpenAI (Stream): {transcript[:100]}...")
    
    # 1. Signal UI to clear previous answer
    answer_queue.put({"type": "clear"})
    
    full_system_prompt = f"{custom_instructions}\n\nRELEVANT CONTEXT (Resume/Job Description):\n{global_context}"
    
    try:
        # Using the streaming API as requested
        # 'stream' is a context manager in the new SDK
        with client.responses.stream(
            model=AI_MODEL,
            input=[
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": f"Question: {transcript}"}
            ],
            max_output_tokens=400,
            temperature=0.6
        ) as response_stream:
            
            print("[DEBUG] Stream started...")
            for event in response_stream:
                if event.type == "response.output_text.delta":
                    token = event.delta
                    if token:
                        answer_queue.put({"type": "text", "content": token})
        
        print("[DEBUG] Stream finished.")
        
    except Exception as e:
        print(f"[DEBUG] OpenAI error: {e}")
        answer_queue.put({"type": "error", "content": f"\n[AI Error: {str(e)}]"})

# ========================
# Audio / WebSocket
# ========================

def on_message(ws, message):
    global current_interim
    try:
        data = json.loads(message)
        msg_type = data.get("type")

        if msg_type == "Turn":
            transcript = data.get("transcript", "")
            end_of_turn = data.get("end_of_turn", False)

            if end_of_turn:
                full = (current_interim + " " + transcript).strip()
                print(f"\n[DEBUG] Final question detected: {full}\n")
                if len(full) > 20:
                    # Start AI generation in a separate thread to not block WebSocket
                    threading.Thread(target=generate_and_stream_response, args=(full,), daemon=True).start()
                current_interim = ""
            else:
                current_interim = transcript
                print(f"[DEBUG] Interim: {transcript}", end="\r", flush=True)
        elif msg_type == "Begin":
            print(f"[DEBUG] AssemblyAI session started: {data.get('id')}")
    except Exception as e:
        print(f"\n[DEBUG] Message error: {e}")

def on_error(ws, error):
    print(f"\n[DEBUG] WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"\n[DEBUG] WebSocket closed.")

def on_open(ws):
    print("[DEBUG] WebSocket connected!")

def websocket_stream():
    global ws
    try:
        url = f"wss://streaming.assemblyai.com/v3/ws?sample_rate={RATE}&format_turns=true&end_of_turn_threshold_ms=3000"
        ws = websocket.WebSocketApp(
            url, header={"Authorization": ASSEMBLYAI_API_KEY},
            on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close
        )
        
        p = pyaudio.PyAudio()
        device_index = None
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev['maxInputChannels'] > 0 and ('CABLE Output' in dev['name'] or 'VB-Audio' in dev['name']):
                device_index = i
                print(f"[DEBUG] Using Audio Device: {dev['name']}")
                break
        
        if device_index is None:
            print("[FATAL] VB-CABLE not found. Using default input.")
            device_index = p.get_default_input_device_info()['index']

        stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, input_device_index=device_index, frames_per_buffer=CHUNK)
        
        wst = threading.Thread(target=ws.run_forever)
        wst.daemon = True
        wst.start()
        time.sleep(1)
        
        while is_running:
            data = stream.read(CHUNK)
            if ws and ws.sock and ws.sock.connected:
                ws.send(data, websocket.ABNF.OPCODE_BINARY)
            else:
                break
    except Exception as e:
        print(f"[DEBUG] Audio Stream Failed: {e}")

# ========================
# GUI Class
# ========================

class StealthCopilotApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Meeting AI Copilot")
        self.geometry("900x600")
        
        self.is_stealth = False
        self.window_visible = True
        
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Sidebar ---
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(4, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Stealth Copilot", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        self.btn_stealth = ctk.CTkButton(self.sidebar_frame, text="Start Stealth Mode", fg_color="red", command=self.activate_stealth_mode)
        self.btn_stealth.grid(row=1, column=0, padx=20, pady=10)

        self.appearance_mode_label = ctk.CTkLabel(self.sidebar_frame, text="Appearance Mode:", anchor="w")
        self.appearance_mode_label.grid(row=5, column=0, padx=20, pady=(10, 0))
        self.appearance_mode_optionemenu = ctk.CTkOptionMenu(self.sidebar_frame, values=["Dark", "Light", "System"],
                                                                       command=self.change_appearance_mode_event)
        self.appearance_mode_optionemenu.grid(row=6, column=0, padx=20, pady=(10, 20))

        # --- Main Content (Tabview) ---
        self.tabview = ctk.CTkTabview(self, corner_radius=10)
        self.tabview.grid(row=0, column=1, padx=20, pady=10, sticky="nsew")
        
        self.tab_setup = self.tabview.add("Setup & Context")
        self.tab_live = self.tabview.add("Live Transcript")

        # --- Setup Tab ---
        self.setup_frame = ctk.CTkFrame(self.tab_setup)
        self.setup_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.lbl_instr = ctk.CTkLabel(self.setup_frame, text="AI Instructions (System Prompt):", anchor="w")
        self.lbl_instr.pack(fill="x", pady=(0, 5))
        self.txt_instr = ctk.CTkTextbox(self.setup_frame, height=100)
        self.txt_instr.pack(fill="x", pady=(0, 10))
        self.txt_instr.insert("0.0", system_instruction_default)
        self.btn_save_instr = ctk.CTkButton(self.setup_frame, text="Update Instructions", command=self.update_instructions)
        self.btn_save_instr.pack(pady=(0, 20))

        self.lbl_uploads = ctk.CTkLabel(self.setup_frame, text="Context Documents (Resume, Job Desc):", anchor="w")
        self.lbl_uploads.pack(fill="x", pady=(0, 5))
        
        self.upload_btn_frame = ctk.CTkFrame(self.setup_frame, fg_color="transparent")
        self.upload_btn_frame.pack(fill="x", pady=(0, 10))
        
        self.btn_upload = ctk.CTkButton(self.upload_btn_frame, text="Upload File (PDF/DOCX/TXT)", command=self.upload_file)
        self.btn_upload.pack(side="left", padx=(0, 10))
        
        self.btn_clear = ctk.CTkButton(self.upload_btn_frame, text="Clear Context", fg_color="gray", command=self.clear_context)
        self.btn_clear.pack(side="left")

        self.lbl_context_preview = ctk.CTkLabel(self.setup_frame, text="Extracted Context Preview:", anchor="w")
        self.lbl_context_preview.pack(fill="x", pady=(0, 5))
        self.txt_context = ctk.CTkTextbox(self.setup_frame)
        self.txt_context.pack(fill="both", expand=True)

        # --- Live Tab ---
        self.live_textbox = ctk.CTkTextbox(self.tab_live, font=("Consolas", 14), text_color="#00ff00", fg_color="black")
        self.live_textbox.pack(fill="both", expand=True)
        
        # --- Stealth Overlay Widget ---
        self.overlay_text_widget = None

        self.start_threads()
        self.after(50, self.check_queue) # Faster polling

    def change_appearance_mode_event(self, new_appearance_mode: str):
        ctk.set_appearance_mode(new_appearance_mode)

    def update_instructions(self):
        global custom_instructions
        custom_instructions = self.txt_instr.get("0.0", "end").strip()
        print("[INFO] Instructions updated.")

    def upload_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("Documents", "*.pdf *.docx *.txt")])
        if file_path:
            text = extract_text_from_file(file_path)
            if text:
                global global_context
                global_context += f"\n--- START FILE: {os.path.basename(file_path)} ---\n{text}\n--- END FILE ---\n"
                self.txt_context.delete("0.0", "end")
                self.txt_context.insert("0.0", global_context)
                print(f"[INFO] Added {file_path} to context.")

    def clear_context(self):
        global global_context
        global_context = ""
        self.txt_context.delete("0.0", "end")

    def start_threads(self):
        threading.Thread(target=websocket_stream, daemon=True).start()
        threading.Thread(target=self.hotkey_listener, daemon=True).start()

    def hotkey_listener(self):
        hotkey = keyboard.HotKey(keyboard.HotKey.parse(HOTKEY), self.toggle_visibility)
        with keyboard.Listener(on_press=lambda k: hotkey.press(k), on_release=hotkey.release) as listener:
            listener.join()

    def toggle_visibility(self):
        self.after(0, self._toggle_visibility_main)

    def _toggle_visibility_main(self):
        if self.window_visible:
            self.withdraw()
        else:
            self.deiconify()
        self.window_visible = not self.window_visible

    def activate_stealth_mode(self):
        self.is_stealth = True
        self.sidebar_frame.grid_forget()
        self.tabview.grid_forget()
        self.grid_columnconfigure(0, weight=1)
        
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.9)
        self.geometry("520x400+100+100")
        
        bg_color = "black"
        fg_color = "lime"
        self.configure(bg=bg_color)
        self.attributes("-transparentcolor", bg_color)

        self.overlay_text = tk.Text(self, bg=bg_color, fg=fg_color, font=("Consolas", 14),
                                   wrap=tk.WORD, relief=tk.FLAT, highlightthickness=0)
        self.overlay_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.overlay_text.insert(tk.END, "Stealth Mode Active.\nWaiting for question...")
        
        self.overlay_text_widget = self.overlay_text

        self.bind("<ButtonPress-1>", self.start_drag)
        self.bind("<ButtonRelease-1>", self.stop_drag)
        self.bind("<B1-Motion>", self.do_drag)

        if sys.platform == "win32":
            hwnd = self.winfo_id()
            try:
                WDA_EXCLUDEFROMCAPTURE = 0x00000011
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
                GWL_EXSTYLE = -20
                WS_EX_APPWINDOW = 0x00040000
                WS_EX_TOOLWINDOW = 0x00000080
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            except Exception as e:
                print(f"[ERROR] Stealth setup failed: {e}")

    def start_drag(self, event): self.x = event.x; self.y = event.y
    def stop_drag(self, event): self.x = None; self.y = None
    def do_drag(self, event):
        if hasattr(self, 'x') and self.x:
            dx = event.x - self.x
            dy = event.y - self.y
            self.geometry(f"+{self.winfo_x() + dx}+{self.winfo_y() + dy}")

    def check_queue(self):
        try:
            while not answer_queue.empty():
                msg = answer_queue.get_nowait()
                self.process_stream_token(msg)
        except:
            pass
        self.after(20, self.check_queue) # Fast update for smooth streaming

    def process_stream_token(self, msg):
        target = self.overlay_text_widget if self.is_stealth else self.live_textbox
        if not target: return

        if msg["type"] == "clear":
            target.delete("1.0", tk.END)
        elif msg["type"] in ["text", "error"]:
            # Smart Autoscroll Check
            # Check if scrollbar is near the bottom BEFORE inserting
            # In Tkinter Text, yview() returns (top_percent, bottom_percent)
            # If bottom_percent is 1.0, we are at the bottom.
            is_at_bottom = target.yview()[1] >= 0.99
            
            target.insert(tk.END, msg["content"])
            
            # If we were at the bottom, keep scrolling. If user scrolled up, don't force it.
            if is_at_bottom:
                target.see(tk.END)

if __name__ == "__main__":
    app = StealthCopilotApp()
    app.mainloop()