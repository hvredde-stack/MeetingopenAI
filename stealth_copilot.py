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
from tkinter import simpledialog

# ========================
# Configuration & Constants
# ========================

import config
OPENAI_API_KEY = config.OPENAI_API_KEY
ASSEMBLYAI_API_KEY = config.ASSEMBLYAI_API_KEY

AI_MODEL = "gpt-4o-mini"  # Faster, lower-latency model
HOTKEY = '<ctrl>+<alt>+h'
HOTKEY_UPLOAD = '<ctrl>+<alt>+u'
HOTKEY_COMPANY = '<ctrl>+<alt>+c'
HOTKEY_STAGE = '<ctrl>+<alt>+s'
DEBUG_MODE = False  # Set to True for verbose logging
CLEAR_ON_NEW_TURN = False  # Keep history so you can scroll
MAX_HISTORY_LINES = 1000  # Trim history to last N lines for performance
STEALTH_HELP_TIMEOUT_MS = 7000  # Hide helper text after a few seconds

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
CONVERSATION_CONTEXT_MAX_TURNS = 100
conversation_history = []

# Context Management
system_instruction_default = """PERSONA:
You are answering as a senior professional with 6-7 years of real-world experience.

CONTEXT:
- You are in a live technical interview.
- Your answers must sound confident, concise, knowledgeable, and experience-driven.
- Speak like someone who has worked on real production systems.
- Avoid textbook explanations and unnecessary theory.
- Structure responses clearly but naturally, as in a spoken interview.
- Do NOT over-explain unless asked.

DOMAIN BEHAVIOR:
- If the question is about DevOps, answer as a DevOps Engineer with 6-7 years of hands-on experience.
- If the domain is not explicitly stated, infer it from the question and respond as a senior professional in that domain.

COMMANDS & CONFIGS:
- Include practical commands only when they add value.
- Keep commands minimal and accurate.
- Explain briefly why the command is used, not how it works.

DISPLAY RULES (STRICT):
1. NO labels like 'Question:', 'Direct Answer:', 'Key Details:'. Just output the answer.
2. UNIFIED RESPONSE: Provide ONE clear, cohesive technical answer.
3. Style:
   - Start immediately with the core answer (Yes/No/Action).
   - If a command is needed, put the command first, then a brief explanation.
   - Otherwise, follow with 1-2 brief sentences of context.
   - Do not repeat phrases or restate the question.
   - Never echo the question.
   - Do not ask clarifying questions; give the best direct answer.
   - If the question names a tool or service, address it directly.
   - Use concrete numbers and examples (e.g., "reduce P99 latency by 20%", "rotate on-call every 5 days").
   - Include what happens next and end with a short prevention step.
   - If using an anecdote, keep it to one short line and stop.
   - Avoid hedging; speak with confidence.
   - Keep it to 2-4 short lines total.
4. Format:
   - Do not use bold or asterisks in the output.
   - Put any commands in a fenced code block with a language hint (e.g., ```bash).
   - Never output a bare language label or raw code without a code fence.
   - No bullet lists unless explicitly asked.
   - Keep it visually clean and short.

STT HANDLING: The user's question is transcribed from audio. If you see obvious typos or phonetic errors (e.g., 'cube control' instead of 'kubectl', 'pro pod' instead of 'production pod'), interpret them logically.

NOISE FILTERING & INTENT:
- The transcript may include background noise, fillers ('um', 'uh'), or preamble ('Okay, moving on...').
- IGNORE these. Focus ONLY on the core technical inquiry.
- If the input is purely conversational noise (e.g., 'Can you hear me?', 'Just a second'), do not generate a technical answer. Output a polite, short status check or nothing.

Goal: Produce an answer that looks clean, readable, and understandable at a glance during a live interview."""

global_context = ""
company_name = ""
interview_stage = ""
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
# AI Interaction (Streaming with Cancellation)
# ========================

def add_to_history(question, answer):
    if not question or not answer:
        return
    conversation_history.append((question.strip(), answer.strip()))
    if len(conversation_history) > CONVERSATION_CONTEXT_MAX_TURNS:
        del conversation_history[:-CONVERSATION_CONTEXT_MAX_TURNS]

def format_conversation_history():
    if not conversation_history:
        return ""
    lines = []
    for idx, (q, a) in enumerate(conversation_history, start=1):
        lines.append(f"Q{idx}: {q}")
        lines.append(f"A{idx}: {a}")
    return "\n".join(lines)

class StreamManager:
    def __init__(self):
        self.generation = 0
        self.current_generation = 0
        self.lock = threading.Lock()
        self.current_thread = None

    def start_new_stream(self, transcript):
        # 1. Bump generation to invalidate any in-flight stream
        with self.lock:
            self.generation += 1
            gen = self.generation
            self.current_generation = gen
        
        # 2. Wait a tiny bit for the old thread to notice and exit
        is_interrupting = False
        if self.current_thread and self.current_thread.is_alive():
            is_interrupting = True
            self.current_thread.join(timeout=0.2)
        
        # 3. CRITICAL: Drain the queue of any "old" tokens that were buffered
        with answer_queue.mutex:
            answer_queue.queue.clear()
            
        # 4. If we interrupted, tell UI to wipe the previous partial turn
        if is_interrupting:
             answer_queue.put({"type": "remove_last_turn", "gen": gen})
        
        # 3. Start the new thread
        self.current_thread = threading.Thread(
            target=self.generate_and_stream_response, 
            args=(transcript, gen),
            daemon=True
        )
        self.current_thread.start()

    def generate_and_stream_response(self, transcript, gen):
        if gen != self.current_generation:
            return
        if DEBUG_MODE:
            print(f"[DEBUG] Sending to OpenAI (Stream): {transcript[:100]}...")
        
        # Send the question as a clean header; avoid extra labels/separators
        answer_queue.put({
            "type": "new_turn",
            "gen": gen,
            "content": f"\n\n{transcript}\n\n"
        })
        
        company_block = f"\n\nCOMPANY:\n{company_name}" if company_name.strip() else ""
        stage_block = f"\n\nINTERVIEW STAGE:\n{interview_stage}" if interview_stage.strip() else ""
        history_text = format_conversation_history()
        history_block = f"\n\nRECENT CONVERSATION:\n{history_text}" if history_text else ""
        full_system_prompt = (
            f"{custom_instructions}\n\nRELEVANT CONTEXT (Resume/Job Description):\n{global_context}"
            f"{company_block}"
            f"{stage_block}"
            f"{history_block}"
        )
        
        full_response_so_far = ""

        try:
            with client.responses.stream(
                model=AI_MODEL,
                input=[
                    {"role": "system", "content": full_system_prompt},
                    {"role": "user", "content": f"{transcript}"}
                ],
                max_output_tokens=120,
                temperature=0.1
            ) as response_stream:
                
                if DEBUG_MODE:
                    print("[DEBUG] Stream started...")
                for event in response_stream:
                    # Stop if a newer turn started
                    if gen != self.current_generation:
                        return

                    if event.type == "response.output_text.delta":
                        token = event.delta
                        if token:
                            full_response_so_far += token
                            
                            # SAFETY: Prevent "split-brain" double answers
                            # If the model tries to output "Direct Answer" a second time, cut it off.
                            # (Removed strict check as prompt no longer uses "Direct Answer" header)
                            # if full_response_so_far.count("Direct Answer") > 1: ...

                            answer_queue.put({"type": "text", "gen": gen, "content": token})
            
            if DEBUG_MODE:
                print("[DEBUG] Stream finished.")
            if gen == self.current_generation and full_response_so_far.strip():
                add_to_history(transcript, full_response_so_far)
            
        except Exception as e:
            if gen == self.current_generation: # Only report error if we weren't cancelled
                print(f"[ERROR] OpenAI error: {e}")
                answer_queue.put({"type": "error", "gen": gen, "content": f"\n[AI Error: {str(e)}]"})

stream_manager = StreamManager()

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
                # The 'transcript' here is the FINAL version for this turn.
                full_question = (current_interim + " " + transcript).strip()
                
                if len(full_question) > 10:
                    print("\n" + "-" * 60)
                    print(f"Question: {full_question}")
                    # Use the manager to start (and safely cancel old) streams
                    stream_manager.start_new_stream(full_question)
                
                current_interim = ""
            else:
                # Accumulate partials so the final question is complete.
                current_interim = (current_interim + " " + transcript).strip()
                display_text = current_interim
                if DEBUG_MODE:
                    print(f"[DEBUG] Hearing: {display_text}", end="\r", flush=True)

        elif msg_type == "Begin":
            print(f"[INFO] AssemblyAI session started: {data.get('id')}")
    except Exception as e:
        if DEBUG_MODE:
            print(f"\n[DEBUG] Message error: {e}")

def on_error(ws, error):
    print(f"\n[ERROR] WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"\n[INFO] WebSocket closed.")

def on_open(ws):
    print("[INFO] WebSocket connected!")

def websocket_stream():
    global ws
    try:
        url = f"wss://streaming.assemblyai.com/v3/ws?sample_rate={RATE}&format_turns=true&end_of_turn_threshold_ms=1600"
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
                print(f"[INFO] Using Audio Device: {dev['name']}")
                break
        
        if device_index is None:
            print("[WARN] VB-CABLE not found. Using default input.")
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
        print(f"[ERROR] Audio Stream Failed: {e}")

# ========================
# GUI Class
# ========================

class StealthCopilotApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Meeting AI Copilot")
        self.geometry("900x600")
        
        self.is_stealth = True  # Default to True
        self.window_visible = True
        self.last_turn_start_index = None
        self.in_code_block = False
        self.code_block_just_opened = False
        
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

        self.lbl_company = ctk.CTkLabel(self.setup_frame, text="Company Name:", anchor="w")
        self.lbl_company.pack(fill="x", pady=(0, 5))
        self.entry_company = ctk.CTkEntry(self.setup_frame, placeholder_text="e.g., Acme Corp")
        self.entry_company.pack(fill="x", pady=(0, 10))
        self.btn_save_company = ctk.CTkButton(self.setup_frame, text="Save Company Name", command=self.update_company_name)
        self.btn_save_company.pack(pady=(0, 20))

        self.lbl_stage = ctk.CTkLabel(self.setup_frame, text="Interview Stage:", anchor="w")
        self.lbl_stage.pack(fill="x", pady=(0, 5))
        self.entry_stage = ctk.CTkEntry(self.setup_frame, placeholder_text="e.g., HR screen / 1st round with manager")
        self.entry_stage.pack(fill="x", pady=(0, 10))
        self.btn_save_stage = ctk.CTkButton(self.setup_frame, text="Save Interview Stage", command=self.update_interview_stage)
        self.btn_save_stage.pack(pady=(0, 20))

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
        
        # Auto-start Stealth Mode
        self.after(100, self.activate_stealth_mode)

    def change_appearance_mode_event(self, new_appearance_mode: str):
        ctk.set_appearance_mode(new_appearance_mode)

    def update_instructions(self):
        global custom_instructions
        custom_instructions = self.txt_instr.get("0.0", "end").strip()
        print("[INFO] Instructions updated.")

    def update_company_name(self, name=None):
        global company_name
        if name is None:
            name = self.entry_company.get().strip()
        company_name = name.strip()
        if company_name:
            print(f"[INFO] Company set to: {company_name}")
        else:
            print("[INFO] Company cleared.")

    def prompt_company_name(self):
        name = simpledialog.askstring("Company Name", "Enter company name:")
        if name is not None:
            self.update_company_name(name)

    def update_interview_stage(self, stage=None):
        global interview_stage
        if stage is None:
            stage = self.entry_stage.get().strip()
        interview_stage = stage.strip()
        if interview_stage:
            print(f"[INFO] Interview stage set to: {interview_stage}")
        else:
            print("[INFO] Interview stage cleared.")

    def prompt_interview_stage(self):
        stage = simpledialog.askstring("Interview Stage", "Enter interview stage (e.g., HR, manager round):")
        if stage is not None:
            self.update_interview_stage(stage)

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

    def upload_file_hotkey(self):
        self.upload_file()

    def clear_context(self):
        global global_context
        global_context = ""
        self.txt_context.delete("0.0", "end")

    def start_threads(self):
        threading.Thread(target=websocket_stream, daemon=True).start()
        threading.Thread(target=self.hotkey_listener, daemon=True).start()

    def hotkey_listener(self):
        hotkeys = {
            HOTKEY: lambda: self.after(0, self.toggle_visibility),
            HOTKEY_UPLOAD: lambda: self.after(0, self.upload_file_hotkey),
            HOTKEY_COMPANY: lambda: self.after(0, self.prompt_company_name),
            HOTKEY_STAGE: lambda: self.after(0, self.prompt_interview_stage),
        }
        with keyboard.GlobalHotKeys(hotkeys) as listener:
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
        self.attributes("-alpha", 0.90)  # Slightly more opaque for better readability
        self.geometry("600x500+100+100") # Larger window
        
        bg_color = "#1e1e1e" # Dark Gray (VS Code style) instead of pure black
        fg_color = "#d4d4d4" # Light Gray text (easier on eyes than lime)
        accent_color = "#007acc" # Blue for headers
        
        self.configure(bg=bg_color)
        self.attributes("-transparentcolor", "#ff00ff") 

        self.overlay_frame = tk.Frame(self, bg=bg_color)
        self.overlay_frame.pack(fill="both", expand=True, padx=2, pady=2)
        
        self.scrollbar = tk.Scrollbar(self.overlay_frame, bg=bg_color)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.overlay_text = tk.Text(self.overlay_frame, bg=bg_color, fg=fg_color, 
                                   font=("Segoe UI", 12), # Cleaner font
                                   wrap=tk.WORD, relief=tk.FLAT, highlightthickness=0,
                                   yscrollcommand=self.scrollbar.set,
                                   padx=15, pady=15, spacing1=5, spacing2=2)
        
        self.overlay_text.tag_config("question", foreground="#569cd6", font=("Segoe UI", 12, "bold")) # Blue bold
        self.overlay_text.tag_config("system", foreground="#6a9955", font=("Consolas", 10)) # Green small
        self.overlay_text.tag_config("code", font=("Consolas", 11), foreground="#ce9178", background="#2d2d2d", lmargin1=20, lmargin2=20, spacing1=5, spacing3=5)
        
        self.overlay_text.pack(side=tk.LEFT, fill="both", expand=True)
        self.scrollbar.config(command=self.overlay_text.yview)
        
        self.overlay_text.insert(tk.END, "Stealth Mode Ready.\n\n", "system")
        self.overlay_text.insert(
            tk.END,
            "- Hold CTRL + Drag to move\n- Auto-scrolling enabled\n- Ctrl+Alt+U: upload context\n- Ctrl+Alt+C: set company\n",
            "system"
        )
        
        self.overlay_text_widget = self.overlay_text

        # Dragging now requires holding Control
        self.bind("<Control-ButtonPress-1>", self.start_drag)
        self.bind("<ButtonRelease-1>", self.stop_drag)
        self.bind("<Control-B1-Motion>", self.do_drag)

        if sys.platform == "win32":
            try:
                import win32gui
                import win32con
                
                # 1. Get the Tkinter content HWND
                hwnd = self.winfo_id()
                
                # 2. Walk up to find the root wrapper (the actual OS window)
                current_hwnd = hwnd
                while True:
                    parent = win32gui.GetParent(current_hwnd)
                    if not parent:
                        break
                    current_hwnd = parent
                
                root_hwnd = current_hwnd
                if DEBUG_MODE:
                    print(f"[DEBUG] Found Root HWND: {root_hwnd} (Base: {hwnd})")

                # 3. Apply Display Affinity to the ROOT HWND
                # 0x00000011 = WDA_EXCLUDEFROMCAPTURE (Hidden from capture, visible to user)
                # 0x00000001 = WDA_MONITOR (Black box in capture) - Use 0x11 for transparency
                user32 = ctypes.windll.user32
                result = user32.SetWindowDisplayAffinity(root_hwnd, 0x00000011)
                
                if result == 0:
                    # If 0x11 failed (older Windows?), try 0x01 (Black Box)
                    if DEBUG_MODE:
                        print("[WARN] WDA_EXCLUDEFROMCAPTURE failed, trying WDA_MONITOR...")
                    user32.SetWindowDisplayAffinity(root_hwnd, 0x00000001)
                else:
                    print(f"[INFO] Stealth Mode Active on HWND {root_hwnd}")

                # 4. Set Tool Window Style (Hide from Alt-Tab/Taskbar)
                ex_style = win32gui.GetWindowLong(root_hwnd, win32con.GWL_EXSTYLE)
                # Remove APPWINDOW, Add TOOLWINDOW
                ex_style = (ex_style & ~win32con.WS_EX_APPWINDOW) | win32con.WS_EX_TOOLWINDOW
                win32gui.SetWindowLong(root_hwnd, win32con.GWL_EXSTYLE, ex_style)
                
                # 5. Force Redraw
                win32gui.SetLayeredWindowAttributes(root_hwnd, 0, 0, win32con.LWA_ALPHA) # Reset to ensure updates apply
                self.attributes("-alpha", 0.90) # Re-apply alpha

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
            # Batch process up to 20 items to reduce GUI overhead and improve smoothness
            for _ in range(20):
                if answer_queue.empty(): break
                msg = answer_queue.get_nowait()
                if "gen" in msg and msg["gen"] != stream_manager.current_generation:
                    continue
                self.process_stream_token(msg)
        except:
            pass
        self.after(20, self.check_queue) # 20ms polling for smoother flow

    def process_stream_token(self, msg):
        target = self.overlay_text_widget if self.is_stealth else self.live_textbox
        if not target: return

        # Smart Autoscroll Check
        # Only autoscroll if the scrollbar is STRICTLY at the bottom (1.0)
        # yview() returns (top, bottom). bottom=1.0 means we are seeing the last line.
        # We use a small epsilon for float comparison safety
        is_at_bottom = target.yview()[1] > 0.999

        if msg["type"] == "new_turn":
            # Mark where this turn starts, so we can delete it if cancelled
            self.in_code_block = False # Reset code block state for new turn
            self.code_block_just_opened = False
            if CLEAR_ON_NEW_TURN:
                try:
                    target.delete("1.0", tk.END)
                except:
                    pass
            try:
                self.last_turn_start_index = target.index("end-1c")
            except:
                self.last_turn_start_index = None
            target.insert(tk.END, msg["content"], "question")
            
        elif msg["type"] == "remove_last_turn":
            if self.last_turn_start_index:
                try:
                    target.delete(self.last_turn_start_index, tk.END)
                except:
                    pass
                self.last_turn_start_index = None

        elif msg["type"] in ["text", "error"]:
            content = msg["content"]
            
            # Simple Markdown Code Block Parser for Streaming
            parts = content.split("```")
            for i, part in enumerate(parts):
                if i > 0:
                    self.in_code_block = not self.in_code_block
                    if self.in_code_block:
                        self.code_block_just_opened = True
                
                if part:
                    tags = "code" if self.in_code_block else None
                    text_part = part
                    if self.in_code_block and self.code_block_just_opened:
                        first_line, sep, rest = text_part.partition("\n")
                        lang = first_line.strip().lower()
                        if lang in {"bash", "sh", "zsh", "powershell", "pwsh", "cmd", "python", "yaml", "yml", "json", "sql"}:
                            text_part = rest
                        self.code_block_just_opened = False
                    target.insert(tk.END, text_part, tags)
            
        # Trim history to the last MAX_HISTORY_LINES lines.
        try:
            line_count = int(target.index("end-1c").split(".")[0])
            if line_count > MAX_HISTORY_LINES:
                trim_to = f"{line_count - MAX_HISTORY_LINES}.0"
                target.delete("1.0", trim_to)
        except:
            pass

        # If we were at the bottom, keep scrolling. If user scrolled up, don't force it.
        if is_at_bottom:
            target.see(tk.END)

if __name__ == "__main__":
    app = StealthCopilotApp()
    app.mainloop()
