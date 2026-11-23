"""Medic: live NVIDIA GPU and CPU dashboard with Core Temp per-core readings."""
from __future__ import annotations
import csv, ctypes, io, math, os, re, shutil, subprocess, threading, time, webbrowser, winreg
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import ttk
try:
    import psutil
except ImportError:
    psutil = None

BG="#0b1220"; PANEL="#101c30"; EDGE="#22324b"; FG="#e7edf7"; MUTED="#8c9ab1"
BLUE="#82aaff"; CYAN="#59d5c8"; AMBER="#f3bd78"; GREEN="#83d8a2"; RED="#ff707b"
FIELDS="name,utilization.gpu,utilization.memory,utilization.encoder,utilization.decoder,temperature.gpu,memory.used,memory.total,power.draw,power.limit,fan.speed,pstate"
KEYS=("name","gpu","mem_util","enc","dec","gpu_temp","vram_used","vram_total","power","power_limit","fan","state")
MAP_NAME="CoreTempMappingObjectEx"

class ProcessorPowerInfo(ctypes.Structure):
    _fields_=[("number",ctypes.c_uint32),("max_mhz",ctypes.c_uint32),
              ("current_mhz",ctypes.c_uint32),("mhz_limit",ctypes.c_uint32),
              ("max_idle_state",ctypes.c_uint32),("current_idle_state",ctypes.c_uint32)]

def read_live_cpu_mhz():
    """Read current per-processor clock values from Windows power telemetry."""
    try:
        count=os.cpu_count() or 1
        rows=ProcessorPowerInfo*count
        api=ctypes.WinDLL("PowrProf.dll").CallNtPowerInformation
        api.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32]
        api.restype=ctypes.c_long
        info=rows()
        status=api(11,None,0,ctypes.byref(info),ctypes.sizeof(info)) # ProcessorInformation
        if status!=0:return None
        active=[float(row.current_mhz) for row in info if row.current_mhz>0]
        return sum(active)/len(active) if active else None
    except Exception:
        return None

class CoreTempData(ctypes.Structure):
    _pack_=4
    _fields_=[
        ("load",ctypes.c_uint32*256),("tjmax",ctypes.c_uint32*128),
        ("core_count",ctypes.c_uint32),("cpu_count",ctypes.c_uint32),
        ("temp",ctypes.c_float*256),("vid",ctypes.c_float),
        ("cpu_speed",ctypes.c_float),("fsb_speed",ctypes.c_float),
        ("multiplier",ctypes.c_float),("cpu_name",ctypes.c_char*100),
        ("fahrenheit",ctypes.c_uint8),("delta_to_tjmax",ctypes.c_uint8),
        ("tdp_supported",ctypes.c_uint8),("power_supported",ctypes.c_uint8),
        ("version",ctypes.c_uint32),("tdp",ctypes.c_uint32*128),
        ("power",ctypes.c_float*128),("multipliers",ctypes.c_float*256),
    ]

class CoreTempReader:
    """Reads Core Temp's documented named shared-memory block."""
    def __init__(self):
        self.kernel32=ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel32.OpenFileMappingW.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_wchar_p]
        self.kernel32.OpenFileMappingW.restype=ctypes.c_void_p
        self.kernel32.MapViewOfFile.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_size_t]
        self.kernel32.MapViewOfFile.restype=ctypes.c_void_p
        self.kernel32.UnmapViewOfFile.argtypes=[ctypes.c_void_p]
        self.kernel32.CloseHandle.argtypes=[ctypes.c_void_p]
        self.handle=None; self.view=None; self.last_try=0; self.last_cpu_mhz=None
    def _open(self):
        if self.view: return True
        if time.monotonic()-self.last_try<2: return False
        self.last_try=time.monotonic()
        handle=self.kernel32.OpenFileMappingW(0x0004,False,MAP_NAME) # FILE_MAP_READ
        if not handle: return False
        view=self.kernel32.MapViewOfFile(handle,0x0004,0,0,ctypes.sizeof(CoreTempData))
        if not view:
            self.kernel32.CloseHandle(handle); return False
        self.handle,self.view=handle,view
        return True
    def read(self):
        if not self._open(): return None
        try:
            d=CoreTempData.from_buffer_copy(ctypes.string_at(self.view,ctypes.sizeof(CoreTempData)))
            speed=float(d.cpu_speed)
            self.last_cpu_mhz=speed if 100<=speed<=10000 else None
            count=min(int(d.core_count),256)
            if count<=0: return None
            temps=[]
            for i in range(count):
                value=float(d.temp[i])
                if d.delta_to_tjmax:
                    per_cpu=max(1,count//max(1,int(d.cpu_count)))
                    value=float(d.tjmax[min(127,i//per_cpu)])-value
                if d.fahrenheit: value=(value-32)*5/9
                if -20<=value<=150: temps.append((i,value))
            return temps or None
        except Exception:
            return None
    def close(self):
        if self.view: self.kernel32.UnmapViewOfFile(self.view); self.view=None
        if self.handle: self.kernel32.CloseHandle(self.handle); self.handle=None

def read_gpu():
    exe=shutil.which("nvidia-smi")
    if not exe:return {"error":"nvidia-smi not found. Check the NVIDIA driver."}
    try:
        p=subprocess.run([exe,"--query-gpu="+FIELDS,"--format=csv,noheader,nounits"],capture_output=True,text=True,timeout=3,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
        return dict(zip(KEYS,(x.strip() for x in next(csv.reader(io.StringIO(p.stdout))))))
    except Exception as e:return {"error":str(e)}

def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):return None

def write_pdf(path,report):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    overview=report["overview"]; core_rows=report["cores"]; concern_rows=report["concerns"]
    navy=colors.HexColor("#17263d"); teal=colors.HexColor("#188f8b"); muted=colors.HexColor("#566579"); pale=colors.HexColor("#f1f4f8"); red=colors.HexColor("#a72e38")
    title_style=ParagraphStyle("MedicTitle",fontName="Helvetica-Bold",fontSize=21,leading=25,textColor=navy,spaceAfter=4)
    subtitle_style=ParagraphStyle("MedicSubtitle",fontName="Helvetica",fontSize=9,leading=13,textColor=muted,spaceAfter=11)
    section_style=ParagraphStyle("MedicSection",fontName="Helvetica-Bold",fontSize=11,leading=14,textColor=teal,spaceBefore=8,spaceAfter=6)
    body_style=ParagraphStyle("MedicBody",fontName="Helvetica",fontSize=7.5,leading=9.5,textColor=navy,alignment=TA_LEFT)
    head_style=ParagraphStyle("MedicTableHead",fontName="Helvetica-Bold",fontSize=7.4,leading=9,textColor=colors.white)
    def cell(value,style=body_style):
        text="" if value is None else str(value)
        text=text.replace("≥",">=").replace("°"," deg ").replace("×","x").replace("·","- ").replace("→","to")
        return Paragraph(escape(text),style)
    def make_table(rows,widths):
        normalized=[list(row)+[""]*max(0,len(widths)-len(row)) for row in rows]
        data=[[cell(value,head_style if r==0 else body_style) for value in row[:len(widths)]] for r,row in enumerate(normalized)]
        table=Table(data,colWidths=widths,repeatRows=1,hAlign="LEFT")
        styles=[
            ("BACKGROUND",(0,0),(-1,0),navy),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,pale]),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#d7dee8")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)
        ]
        if len(rows)>1 and len(rows[1])==1:styles.append(("SPAN",(0,1),(-1,1)))
        table.setStyle(TableStyle(styles))
        return table
    doc=SimpleDocTemplate(path,pagesize=letter,leftMargin=0.55*inch,rightMargin=0.55*inch,topMargin=0.5*inch,bottomMargin=0.48*inch,title="Medic Session Statistics",author="Medic")
    story=[Paragraph("Medic Session Report",title_style),Paragraph("Recorded hardware telemetry - temperature concern threshold: 80 deg C",subtitle_style),Paragraph("SESSION OVERVIEW",section_style),make_table(overview,[2.15*inch,1.25*inch,3.55*inch]),Spacer(1,10),Paragraph("CPU CORE TEMPERATURES",section_style),make_table(core_rows,[0.62*inch,0.83*inch,0.73*inch,1.24*inch,0.62*inch,0.93*inch,0.98*inch]),PageBreak(),Paragraph("TEMPERATURE CONCERN AREAS",title_style),Paragraph("Each CPU core is tracked independently. GPU and CPU-core readings at or above 80 deg C are listed below.",subtitle_style),make_table(concern_rows,[0.95*inch,1.28*inch,1.28*inch,0.82*inch,0.78*inch,0.84*inch]),Spacer(1,12),Paragraph("GPU board power and estimated system draw are telemetry estimates, not a power-supply sensor reading.",body_style)]
    def footer(canvas,document):
        canvas.saveState(); canvas.setStrokeColor(colors.HexColor("#d7dee8")); canvas.line(0.55*inch,0.36*inch,letter[0]-0.55*inch,0.36*inch)
        canvas.setFont("Helvetica",7); canvas.setFillColor(muted); canvas.drawString(0.55*inch,0.22*inch,"MEDIC  /  SESSION TELEMETRY"); canvas.drawRightString(letter[0]-0.55*inch,0.22*inch,f"PAGE {document.page}"); canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)

def build_record_report(samples,duration,started_at=None):
    def avg(values):return sum(values)/len(values) if values else None
    def timed_extreme(key,highest=True):
        entries=[(s.get(key),s) for s in samples if s.get(key) is not None]
        return (max(entries,key=lambda x:x[0]) if highest else min(entries,key=lambda x:x[0])) if entries else (None,None)
    core_ids=sorted({core_id for sample in samples for core_id,_ in sample.get("cores",[])})
    sources=[("GPU",lambda s:s.get("gpu_temp"))]
    sources.extend((f"CPU Core {core_id}",lambda s,cid=core_id:next((v for i,v in s.get("cores",[]) if i==cid),None)) for core_id in core_ids)
    concerns=[]
    for source,get_value in sources:
        active=None
        for index,sample in enumerate(samples):
            value=get_value(sample); hot=value is not None and value>=80
            if hot:
                if active is None:
                    active={"source":source,"start":sample["time"],"end":sample["time"],"peak":value,"peak_time":sample["time"],"duration":0.0}
                if value>active["peak"]:active["peak"]=value; active["peak_time"]=sample["time"]
                next_elapsed=samples[index+1]["elapsed"] if index+1<len(samples) else duration
                active["duration"]+=max(0.0,min(duration,next_elapsed)-sample["elapsed"])
                active["end"]=samples[index+1]["time"] if index+1<len(samples) else sample["time"]
            elif active is not None:
                concerns.append(active); active=None
        if active is not None:concerns.append(active)
    gpu_temps=[s["gpu_temp"] for s in samples if s.get("gpu_temp") is not None]
    all_core_temps=[(temp,s) for s in samples for _,temp in s.get("cores",[]) if temp is not None]
    hottest_per_sample=[(max(s["cores"],key=lambda x:x[1])[1],s) for s in samples if s.get("cores")]
    core_hot_seconds={core_id:sum(c["duration"] for c in concerns if c["source"]==f"CPU Core {core_id}") for core_id in core_ids}
    gpu_hot_seconds=sum(c["duration"] for c in concerns if c["source"]=="GPU")
    cpu_hot_seconds=sum(core_hot_seconds.values())
    rows=[["STATISTIC","VALUE","TIME / DETAILS"]]
    def add(name,value,detail=""):
        rows.append([name,value,detail])
    add("Recording duration (seconds)",round(duration,2))
    add("Recording started at",started_at.isoformat(timespec="seconds") if started_at else "")
    add("Samples captured",len(samples))
    add("RAM kit","POWER X · 16 GB × 2")
    add("Average GPU temperature (°C)",round(avg(gpu_temps),2) if gpu_temps else None)
    peak_gpu,peak_gpu_sample=timed_extreme("gpu_temp"); add("Highest GPU temperature (°C)",round(peak_gpu,2) if peak_gpu is not None else None,peak_gpu_sample["time"] if peak_gpu_sample else "")
    low_gpu,low_gpu_sample=timed_extreme("gpu_temp",False); add("Lowest GPU temperature (°C)",round(low_gpu,2) if low_gpu is not None else None,low_gpu_sample["time"] if low_gpu_sample else "")
    add("GPU time at ≥80°C (seconds)",round(gpu_hot_seconds,2))
    add("Average CPU core temperature (°C)",round(avg([v for v,_ in all_core_temps]),2) if all_core_temps else None)
    add("Average hottest CPU core per sample (°C)",round(avg([v for v,_ in hottest_per_sample]),2) if hottest_per_sample else None)
    if all_core_temps:
        hottest_core,hottest_sample=max(all_core_temps,key=lambda x:x[0]); core_id=next((i for i,v in hottest_sample["cores"] if v==hottest_core),None)
        add("Highest CPU core temperature (°C)",round(hottest_core,2),f"Core {core_id} at {hottest_sample['time']}")
    else:add("Highest CPU core temperature (°C)",None)
    add("Total CPU core time at ≥80°C (core-seconds)",round(cpu_hot_seconds,2))
    for key,label in (("gpu_load","GPU"),("cpu_load","CPU")):
        values=[s[key] for s in samples if s.get(key) is not None]
        add(f"Average {label} load (%)",round(avg(values),2) if values else None)
        peak,peak_sample=timed_extreme(key); add(f"Peak {label} load (%)",round(peak,2) if peak is not None else None,peak_sample["time"] if peak_sample else "")
    combined=[((s["gpu_load"]+s["cpu_load"])/2,s) for s in samples if s.get("gpu_load") is not None and s.get("cpu_load") is not None]
    if combined:
        val,sample=max(combined,key=lambda x:x[0]); add("Peak combined load (mean CPU/GPU %)",round(val,2),sample["time"])
    else:add("Peak combined load (mean CPU/GPU %)",None)
    for key,label in (("gpu_power","GPU board power (W)"),("fan","GPU fan speed (%)"),("cpu_ghz","CPU speed (GHz)"),("vram_used","VRAM used (GB)"),("ram_used","System RAM used (GB)"),("ram_load","System RAM load (%)"),("estimated_power","Estimated system draw (W)")):
        values=[s[key] for s in samples if s.get(key) is not None]
        add(f"Average {label}",round(avg(values),2) if values else None)
        peak,peak_sample=timed_extreme(key); add(f"Peak {label}",round(peak,2) if peak is not None else None,peak_sample["time"] if peak_sample else "")
    core_rows=[["CORE","AVERAGE °C","PEAK °C","PEAK TIME","LOW °C","TIME ≥80°C (s)","READINGS ≥80°C"]]
    for core_id in core_ids:
        vals=[(temp,s) for s in samples for i,temp in s.get("cores",[]) if i==core_id and temp is not None]
        if not vals:continue
        peak,peak_sample=max(vals,key=lambda x:x[0]); low=min(v for v,_ in vals)
        hot_count=sum(1 for value,_ in vals if value>=80)
        core_rows.append([f"Core {core_id}",round(avg([v for v,_ in vals]),2),round(peak,2),peak_sample["time"],round(low,2),round(core_hot_seconds.get(core_id,0),2),hot_count])
    concern_rows=[["SOURCE","START","END","DURATION (s)","PEAK (°C)","PEAK TIME"]]
    concern_rows.extend([[c["source"],c["start"],c["end"],round(c["duration"],2),round(c["peak"],2),c["peak_time"]] for c in concerns])
    if not concerns:concern_rows.append(["No dark-red temperature readings (≥80°C) recorded."])
    return {"overview":rows,"cores":core_rows,"concerns":concern_rows},concerns

def temp_color(value):
    try:
        v=float(value)
        return BLUE if v<30 else "#a3e6a1" if v<40 else CYAN if v<50 else "#fff0a6" if v<60 else "#ffb875" if v<70 else "#ff8585" if v<80 else "#b71c1c"
    except (TypeError,ValueError):return MUTED

def read_cpu_name():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name=winreg.QueryValueEx(key,"ProcessorNameString")[0].strip()
            name=re.sub(r"Intel\(R\)\s*Core\(TM\)","",name,flags=re.IGNORECASE)
            name=re.sub(r"Intel\s+Core", "", name, flags=re.IGNORECASE)
            return re.sub(r"\s+"," ",name).strip(" -")
    except OSError:
        return "CPU model unavailable"

class App:
    def __init__(self,root):
        self.root=root; self.recording_active=False; self.record_samples=[]; self.record_started_at=None; self.record_wall_start=None; root.title("Medic | Live Hardware Monitor"); root.geometry("960x700"); root.minsize(850,400); root.configure(bg=BG)
        self.running=True; self.started_at=time.monotonic(); self.poll_interval=3; self.poll_changed=threading.Event(); self.poll_buttons={}; self.cpu_model=read_cpu_name(); self.psu_watts=750; self.data={}; self.samples=deque(); self.lock=threading.Lock(); self.core_labels={}; self.core_accents={}; self.core_wait_label=None; self.history_enabled=False; self.history_mode="both"; self.history_buttons={}; self.history_range="5m"; self.history_range_buttons={}
        self.core_reader=CoreTempReader()
        if psutil:psutil.cpu_percent(interval=None)
        self.build(); root.protocol("WM_DELETE_WINDOW",self.close)
        threading.Thread(target=self.poll,daemon=True).start(); self.refresh()
    def card(self,parent,title,color):
        f=tk.Frame(parent,bg=PANEL,highlightthickness=0,padx=14,pady=11)
        tk.Frame(f,bg=color,height=2).pack(fill="x",pady=(0,9))
        tk.Label(f,text=title,bg=PANEL,fg=color,font=("Segoe UI",9,"bold")).pack(anchor="w")
        tk.Frame(f,bg="#1d2b42",height=1).pack(fill="x",pady=(7,0))
        return f
    def metric_pair(self,parent,left_title,left_color,right_title,right_color):
        row=tk.Frame(parent,bg=PANEL); row.pack(fill="x",pady=(11,10))
        a=tk.Frame(row,bg=PANEL); a.pack(side="left",fill="x",expand=True)
        tk.Frame(row,bg=EDGE,width=1).pack(side="left",fill="y",pady=6)
        b=tk.Frame(row,bg=PANEL); b.pack(side="left",fill="x",expand=True,padx=(10,0))
        tk.Label(a,text=left_title,bg=PANEL,fg=MUTED,font=("Segoe UI",10,"bold")).pack(anchor="w")
        tk.Label(b,text=right_title,bg=PANEL,fg=MUTED,font=("Segoe UI",10,"bold")).pack(anchor="w")
        va=tk.Label(a,text="—",bg=PANEL,fg=left_color,font=("Bahnschrift",26,"bold")); va.pack(anchor="w")
        vb=tk.Label(b,text="—",bg=PANEL,fg=right_color,font=("Bahnschrift",26,"bold")); vb.pack(anchor="w")
        return va,vb
    def line(self,parent,title):
        r=tk.Frame(parent,bg=PANEL); r.pack(fill="x",pady=2)
        tk.Label(r,text=title,bg=PANEL,fg=MUTED,font=("Segoe UI",11)).pack(side="left",pady=3)
        v=tk.Label(r,text="—",bg=PANEL,fg=FG,font=("Bahnschrift",11,"bold"),anchor="e",justify="right",wraplength=210); v.pack(side="right",pady=3)
        tk.Frame(r,bg="#1b2a40",height=1).pack(side="bottom",fill="x")
        return v
    def build(self):
        h=tk.Frame(self.root,bg=BG); h.pack(fill="x",padx=22,pady=(12,10))
        brand=tk.Frame(h,bg=BG); brand.pack(side="left")
        mark=tk.Canvas(brand,width=34,height=24,bg=BG,highlightthickness=0); mark.pack(side="left",padx=(0,8))
        mark.create_line(1,13,7,13,10,8,13,18,17,3,20,13,32,13,fill=CYAN,width=2,smooth=True,capstyle="round",joinstyle="round")
        brand_text=tk.Frame(brand,bg=BG); brand_text.pack(side="left")
        name_row=tk.Frame(brand_text,bg=BG); name_row.pack(anchor="w")
        tk.Label(name_row,text="Medic",bg=BG,fg=FG,font=("Segoe UI",19,"bold")).pack(side="left")
        tk.Label(name_row,text="+",bg=BG,fg="#ff5c68",font=("Segoe UI",26,"bold")).pack(side="left",padx=(2,0),pady=(0,1))
        poll_row=tk.Frame(brand_text,bg=BG); poll_row.pack(anchor="w",pady=(1,0))
        tk.Label(poll_row,text="POLL ADJUSTER",bg=BG,fg=MUTED,font=("Segoe UI",8,"bold")).pack(side="left",padx=(0,7))
        for seconds,label in ((1,"1 SEC"),(3,"3 SEC"),(5,"5 SEC"),(10,"10 SEC")):
            button=tk.Button(poll_row,text=label,bg="#223b45" if seconds==self.poll_interval else PANEL,fg=CYAN if seconds==self.poll_interval else MUTED,activebackground="#223b45",activeforeground=CYAN,relief="solid",bd=1,highlightthickness=0,cursor="hand2",font=("Segoe UI",9,"bold"),padx=6,pady=2,command=lambda value=seconds:self.set_poll_interval(value))
            button.pack(side="left",padx=(0,4)); self.poll_buttons[seconds]=button
        self.session_clock=tk.Label(h,text="00:00:00",bg=PANEL,fg=FG,font=("Bahnschrift",10,"bold"),padx=9,pady=5); self.session_clock.pack(side="right",padx=(10,0),pady=(4,0))
        self.status=tk.Label(h,text="● CONNECTING",bg="#2a2419",fg=AMBER,font=("Segoe UI",9,"bold"),padx=9,pady=5); self.status.pack(side="right",pady=(4,0))
        self.speedtest_image=tk.PhotoImage(file=os.path.join(os.path.dirname(os.path.abspath(__file__)),"speedtest_button_cutout.png"))
        self.speedtest_button=tk.Button(h,image=self.speedtest_image,bg=BG,activebackground=BG,relief="flat",bd=0,highlightthickness=0,cursor="hand2",command=lambda:webbrowser.open("https://www.speedtest.net/"))
        self.speedtest_button.pack(side="right",padx=(0,14),pady=(4,0))
        self.record_button=tk.Button(h,text="RECORD",bg=PANEL,fg=CYAN,activebackground="#23314b",activeforeground=FG,relief="solid",bd=1,highlightthickness=1,highlightbackground=EDGE,cursor="hand2",font=("Segoe UI",10,"bold"),padx=9,pady=5,command=self.start_recording)
        self.record_button.pack(side="right",padx=(0,8),pady=(4,0))
        cards=tk.Frame(self.root,bg=BG); cards.pack(fill="x",padx=17)
        self.gpu=self.card(cards,"GRAPHICS PROCESSOR",BLUE); self.cpu=self.card(cards,"CENTRAL PROCESSOR",CYAN); self.ram=self.card(cards,"SYSTEM MEMORY",AMBER)
        for f in (self.gpu,self.cpu,self.ram):f.pack(side="left",fill="both",expand=True,padx=5)
        self.gpu_header=tk.Label(self.gpu,text="NVIDIA GPU",bg=PANEL,fg="#76b900",font=("Segoe UI",12,"bold"),anchor="w",justify="left",wraplength=210)
        self.gpu_header.pack(anchor="w",pady=(5,0))
        self.cpu_header=tk.Label(self.cpu,text=self.cpu_model,bg=PANEL,fg="#0071c5",font=("Segoe UI",12,"bold"),anchor="w",justify="left",wraplength=210)
        self.cpu_header.pack(anchor="w",pady=(5,0))
        self.ram_header=tk.Label(self.ram,text="POWER X  ·  16 GB × 2",bg=PANEL,fg=AMBER,font=("Segoe UI",12,"bold"),anchor="w",justify="left",wraplength=210)
        self.ram_header.pack(anchor="w",pady=(5,0))
        self.gpu_use,self.gpu_temp=self.metric_pair(self.gpu,"GPU LOAD",BLUE,"GPU TEMP",AMBER)
        self.cpu_use,self.cpu_temp=self.metric_pair(self.cpu,"CPU LOAD",CYAN,"CPU TEMP",AMBER)
        self.ram_used,self.ram_load=self.metric_pair(self.ram,"RAM IN USE",AMBER,"RAM LOAD",BLUE)
        self.fan=self.line(self.gpu,"Fan speed"); self.vram=self.line(self.gpu,"Video memory")
        self.power_estimate=self.line(self.cpu,"Estimated system draw")
        self.cpu_speed=self.line(self.cpu,"CPU speed")
        self.ram_avail=self.line(self.ram,"Available"); self.ram_total=self.line(self.ram,"Total")
        core_panel=tk.Frame(self.root,bg=PANEL,highlightthickness=0); core_panel.pack(fill="x",padx=22,pady=(12,8))
        head=tk.Frame(core_panel,bg=PANEL); head.pack(fill="x",padx=15,pady=(10,2))
        tk.Label(head,text="CPU Core Temperatures",bg=PANEL,fg=FG,font=("Segoe UI",11,"bold")).pack(side="left")
        tk.Label(head,text="Live readings from Core Temp 1.18",bg=PANEL,fg=MUTED,font=("Segoe UI",10)).pack(side="right")
        tk.Frame(core_panel,bg=CYAN,height=2).pack(fill="x")
        self.cores_frame=tk.Frame(core_panel,bg=PANEL); self.cores_frame.pack(fill="x",padx=12,pady=(3,9))
        chart=tk.Frame(self.root,bg=PANEL,highlightthickness=0); self.chart=chart; chart.pack(fill="both" if self.history_enabled else "x",expand=self.history_enabled,padx=22,pady=(0,7))
        tk.Frame(chart,bg="#425a7a",height=2).pack(fill="x")
        head=tk.Frame(chart,bg=PANEL); head.pack(fill="x",padx=16,pady=(8,0))
        tk.Label(head,text="LOAD HISTORY",bg=PANEL,fg=FG,font=("Segoe UI",10,"bold")).pack(side="left")
        self.history_toggle=tk.Button(head,text="HISTORY OFF",bg=PANEL,fg=MUTED,activebackground="#23314b",activeforeground=FG,relief="solid",bd=1,highlightthickness=0,cursor="hand2",font=("Segoe UI",9,"bold"),padx=6,pady=2,command=self.toggle_history)
        self.history_toggle.pack(side="right",padx=(4,0))
        ranges=tk.Frame(head,bg=PANEL); ranges.pack(side="right")
        for key,label in (("5m","5 MIN"),("30m","30 MIN"),("1h","1 HOUR"),("session","SESSION")):
            button=tk.Button(ranges,text=label,bg="#23314b" if key==self.history_range else PANEL,fg=FG if key==self.history_range else MUTED,activebackground="#23314b",activeforeground=FG,relief="solid",bd=1,highlightthickness=0,cursor="hand2",font=("Segoe UI",9,"bold"),padx=6,pady=2,command=lambda value=key:self.set_history_range(value))
            button.pack(side="left",padx=(4,0)); self.history_range_buttons[key]=button
        filters=tk.Frame(head,bg=PANEL); filters.pack(side="right",padx=(0,8))
        for mode,label,color in (("both","BOTH",FG),("gpu","GPU",BLUE),("cpu","CPU",CYAN)):
            button=tk.Button(filters,text=label,bg="#23314b" if mode==self.history_mode else PANEL,fg=color if mode==self.history_mode else MUTED,activebackground="#23314b",activeforeground=color,relief="solid",bd=1,highlightthickness=0,cursor="hand2",font=("Segoe UI",9,"bold"),padx=7,pady=2,command=lambda value=mode:self.set_history_mode(value))
            button.pack(side="left",padx=(4,0)); self.history_buttons[mode]=button
        self.plot=tk.Canvas(chart,bg=PANEL,highlightthickness=0,height=126)
        if self.history_enabled:self.plot.pack(fill="both",expand=True,padx=10,pady=(1,5))
        footer=tk.Frame(self.root,bg=BG); footer.pack(fill="x",padx=24,pady=(0,7))
        tk.Frame(footer,bg=CYAN,width=3,height=13).pack(side="left",padx=(0,8))
        self.core_feed_status=tk.Label(footer,text="CPU SENSOR  /  WAITING FOR CORE TEMP",bg=BG,fg=MUTED,font=("Segoe UI",9,"bold")); self.core_feed_status.pack(side="left")
        tk.Label(footer,text="MEDIC  ·  LOCAL MONITOR",bg=BG,fg="#58677f",font=("Segoe UI",8,"bold")).pack(side="right")
        self.root.update_idletasks(); self.full_window_geometry=self.root.geometry()
        if not self.history_enabled:self.root.geometry(f"{self.root.winfo_width()}x{self.root.winfo_reqheight()}")
    def toggle_history(self):
        self.history_enabled=not self.history_enabled
        if self.history_enabled:
            self.chart.pack_forget(); self.chart.pack(fill="both",expand=True,padx=22,pady=(0,7))
            self.plot.pack(fill="both",expand=True,padx=10,pady=(1,5)); self.history_toggle.config(text="HISTORY ON",fg=CYAN)
            self.root.geometry(self.full_window_geometry)
        else:
            self.plot.delete("all"); self.plot.pack_forget(); self.chart.pack_forget(); self.chart.pack(fill="x",expand=False,padx=22,pady=(0,7)); self.history_toggle.config(text="HISTORY OFF",fg=MUTED)
            self.root.update_idletasks()
            self.root.geometry(f"{self.root.winfo_width()}x{self.root.winfo_reqheight()}")
    def poll(self):
        while self.running:
            self.poll_changed.clear()
            cycle_started=time.monotonic()
            d=read_gpu()
            if psutil:
                d["cpu"]=psutil.cpu_percent(interval=None); m=psutil.virtual_memory(); d["ram_used"]=m.used/1024**3; d["ram_total"]=m.total/1024**3; d["ram_pct"]=m.percent
                d["cores"]=psutil.cpu_count(logical=False); d["threads"]=psutil.cpu_count(logical=True)
            d["core_temps"]=self.core_reader.read()
            d["cpu_freq"]=self.core_reader.last_cpu_mhz or read_live_cpu_mhz()
            elapsed=time.monotonic()-self.started_at
            with self.lock:
                self.data=d
                if self.history_enabled:self.samples.append((elapsed,d.get("gpu"),d.get("cpu")))
                if self.recording_active:
                    record_elapsed=time.monotonic()-self.record_started_at
                    self.record_samples.append(self.make_record_sample(d,record_elapsed))
            delay=max(0.0,self.poll_interval-(time.monotonic()-cycle_started))
            self.poll_changed.wait(delay)
    def set_poll_interval(self,seconds):
        self.poll_interval=int(seconds)
        for interval,button in self.poll_buttons.items():
            selected=interval==self.poll_interval
            button.config(bg="#223b45" if selected else PANEL,fg=CYAN if selected else MUTED)
        self.poll_changed.set()
    def make_record_sample(self,d,elapsed):
        cores=[]
        for core_id,temp in (d.get("core_temps") or []):
            value=number(temp)
            if value is not None:cores.append((int(core_id)+1,value))
        gpu_power=number(d.get("power")); cpu_load=number(d.get("cpu"))
        return {"elapsed":elapsed,"time":datetime.now().astimezone().isoformat(timespec="seconds"),
                "gpu_load":number(d.get("gpu")),"gpu_temp":number(d.get("gpu_temp")),"cpu_load":cpu_load,
                "cores":cores,"cpu_ghz":number(d.get("cpu_freq"))/1000 if number(d.get("cpu_freq")) is not None else None,
                "fan":number(d.get("fan")),"gpu_power":gpu_power,
                "vram_used":number(d.get("vram_used"))/1000 if number(d.get("vram_used")) is not None else None,
                "vram_total":number(d.get("vram_total"))/1000 if number(d.get("vram_total")) is not None else None,
                "ram_used":number(d.get("ram_used")),"ram_total":number(d.get("ram_total")),"ram_load":number(d.get("ram_pct")),
                "estimated_power":self.estimate_system_power(gpu_power,cpu_load)}
    def start_recording(self):
        with self.lock:
            self.record_samples.clear(); self.record_started_at=time.monotonic(); self.record_wall_start=datetime.now().astimezone(); self.recording_active=True
        self.record_button.config(text="STOP RECORDING",bg="#321a27",fg="#ff707b",activebackground="#42212e",highlightbackground="#60313d",command=self.stop_recording)
        self.poll_changed.set()
    def stop_recording(self):
        if not self.recording_active:return
        with self.lock:
            duration=max(0.0,time.monotonic()-self.record_started_at); self.recording_active=False; samples=list(self.record_samples)
        self.record_button.config(text="EXPORTING…",state="disabled")
        report,concerns=build_record_report(samples,duration,self.record_wall_start)
        popup=tk.Toplevel(self.root); popup.title("Medic · Exporting PDF"); popup.configure(bg=BG); popup.geometry("520x410"); popup.minsize(460,360); popup.transient(self.root)
        tk.Label(popup,text="SESSION REPORT",bg=BG,fg=FG,font=("Segoe UI",13,"bold")).pack(anchor="w",padx=18,pady=(16,2))
        status=tk.Label(popup,text="Preparing your PDF report…",bg=BG,fg=CYAN,font=("Segoe UI",10,"bold")); status.pack(anchor="w",padx=18,pady=(0,8))
        tk.Label(popup,text="AREAS OF CONCERN  /  DARK RED = 80°C+",bg=PANEL,fg=RED,font=("Segoe UI",9,"bold"),anchor="w",padx=10,pady=7).pack(fill="x",padx=14)
        body=tk.Frame(popup,bg=PANEL); body.pack(fill="both",expand=True,padx=14,pady=(0,9))
        concerns_text=tk.Text(body,bg=PANEL,fg=FG,insertbackground=FG,relief="flat",wrap="word",font=("Segoe UI",10),height=9,padx=9,pady=8)
        scrollbar=tk.Scrollbar(body,command=concerns_text.yview); concerns_text.configure(yscrollcommand=scrollbar.set)
        concerns_text.pack(side="left",fill="both",expand=True); scrollbar.pack(side="right",fill="y")
        if concerns:
            for event in concerns:
                start=event["start"].replace("T"," "); end=event["end"].replace("T"," ")
                concerns_text.insert("end",f"{event['source']}  ·  peak {event['peak']:.1f}°C at {event['peak_time'].replace('T',' ')}\n  {start} → {end}  ({event['duration']:.0f}s above 80°C)\n\n")
        else:
            has_temperature_data=any(s.get("gpu_temp") is not None or s.get("cores") for s in samples)
            concerns_text.insert("end","No recorded GPU or CPU-core temperatures reached 80°C." if has_temperature_data else "No GPU or CPU-core temperature samples were available during this recording.")
            if not any(s.get("cores") for s in samples):concerns_text.insert("end","\n\nCPU-core readings require Core Temp to be running.")
            else:concerns_text.insert("end","\n\nEvery CPU core was tracked separately.")
        concerns_text.configure(state="disabled")
        progress=ttk.Progressbar(popup,mode="indeterminate",length=240); progress.pack(fill="x",padx=18,pady=(0,10)); progress.start(12)
        actions=tk.Frame(popup,bg=BG); actions.pack(fill="x",padx=18,pady=(0,14))
        close_button=tk.Button(actions,text="CLOSE",state="disabled",command=popup.destroy,bg=PANEL,fg=MUTED,relief="solid",bd=1,highlightthickness=0,padx=12,pady=5)
        close_button.pack(side="right")
        open_button=tk.Button(actions,text="OPEN PDF",state="disabled",bg="#172b34",fg=CYAN,relief="solid",bd=1,highlightthickness=0,padx=12,pady=5)
        open_button.pack(side="right",padx=(0,8))
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S"); output=os.path.join(os.path.expanduser("~"),"Downloads",f"Medic_Statistics_{stamp}.pdf")
        def finish(error=None):
            progress.stop(); close_button.config(state="normal")
            if error:
                status.config(text=f"Export failed: {error}",fg=RED)
                self.record_button.config(text="RECORD",state="normal",bg=PANEL,fg=CYAN,highlightbackground=EDGE,command=self.start_recording)
                return
            status.config(text=f"Saved PDF to Downloads: {os.path.basename(output)}",fg=GREEN)
            open_button.config(state="normal",command=lambda:os.startfile(output))
            self.record_button.config(text="RECORD",state="normal",bg=PANEL,fg=CYAN,highlightbackground=EDGE,command=self.start_recording)
        def export_worker():
            try:
                os.makedirs(os.path.dirname(output),exist_ok=True); write_pdf(output,report)
                self.root.after(0,finish)
            except Exception as error:
                try:self.root.after(0,lambda message=str(error):finish(message))
                except tk.TclError:pass
        threading.Thread(target=export_worker,daemon=True).start()
    @staticmethod
    def fmt(value,suffix="",digits=0):
        try:return f"{float(value):.{digits}f}{suffix}"
        except (TypeError,ValueError):return "—"
    @staticmethod
    def fmt_vram(value):
        try:return f"{float(value)/1000:.1f} GB"
        except (TypeError,ValueError):return "—"
    @staticmethod
    def fmt_ghz(value):
        try:return f"{float(value)/1000:.2f} GHz"
        except (TypeError,ValueError):return "—"
    @staticmethod
    def estimate_system_power(gpu_w,cpu_load):
        try:
            gpu_w=max(0.0,float(gpu_w)); cpu_load=max(0.0,min(100.0,float(cpu_load)))
            cpu_w=15.0+1.1*cpu_load
            return gpu_w+cpu_w+50.0
        except (TypeError,ValueError):return None
    def update_core_tiles(self,temps):
        if temps is None:
            if self.core_wait_label is None:
                for child in self.cores_frame.winfo_children():child.destroy()
                self.core_labels.clear(); self.core_accents.clear()
                self.core_wait_label=tk.Label(self.cores_frame,text="NO CPU SENSOR FEED  /  START CORE TEMP",bg=PANEL,fg=MUTED,font=("Bahnschrift",10,"bold"))
                self.core_wait_label.grid(row=0,column=0,sticky="w",padx=5,pady=8)
            return
        if self.core_wait_label is not None:
            self.core_wait_label.destroy(); self.core_wait_label=None
        ids={i for i,_ in temps}
        if ids!=set(self.core_labels):
            for child in self.cores_frame.winfo_children():child.destroy()
            self.core_labels.clear(); self.core_accents.clear()
            for n,(i,_) in enumerate(temps):
                col=n%8; row=n//8
                tile=tk.Frame(self.cores_frame,bg="#15243a",highlightthickness=0,padx=8,pady=4)
                tile.grid(row=row,column=col,sticky="ew",padx=3,pady=3)
                self.cores_frame.grid_columnconfigure(col,weight=1)
                accent=tk.Frame(tile,bg=AMBER,width=3); accent.pack(side="left",fill="y",padx=(0,8))
                body=tk.Frame(tile,bg="#15243a"); body.pack(side="left",fill="x",expand=True)
                tk.Label(body,text=f"CORE {i+1:02}",bg="#15243a",fg=MUTED,font=("Segoe UI",9,"bold")).pack(anchor="w")
                value=tk.Label(body,text="— °C",bg="#15243a",fg=AMBER,font=("Bahnschrift",18,"bold")); value.pack(anchor="w")
                self.core_labels[i]=value
                self.core_accents[i]=accent
        for i,t in temps:
            if i in self.core_labels:
                color=temp_color(t); self.core_labels[i].config(text=f"{t:.0f} °C",fg=color); self.core_accents[i].config(bg=color)
    def refresh(self):
        with self.lock:d=self.data.copy(); hist=list(self.samples) if self.history_enabled else []
        if d:
            live="error" not in d
            if self.recording_active:
                self.status.config(text="● RECORDING",fg="#ff707b",bg="#321a27")
            else:
                self.status.config(text="● LIVE" if live else "● GPU UNAVAILABLE",fg=GREEN if live else AMBER,bg="#132a2a" if live else "#2a2419")
            self.gpu_use.config(text=self.fmt(d.get("gpu"),"%"),fg="#76b900"); gt=d.get("gpu_temp"); self.gpu_temp.config(text=self.fmt(gt," °C",0),fg=temp_color(gt))
            self.cpu_use.config(text=self.fmt(d.get("cpu"),"%"),fg="#0071c5"); ct=d.get("core_temps"); hottest=max((t for _,t in ct),default=None) if ct else None
            self.cpu_temp.config(text=self.fmt(hottest," °C") if hottest is not None else "— °C",fg=temp_color(hottest))
            self.core_feed_status.config(text="CPU SENSOR  /  CORE TEMP FEED LIVE" if ct else "CPU SENSOR  /  WAITING FOR CORE TEMP",fg=CYAN if ct else MUTED)
            ram_used_gb=d.get("ram_used")
            ram_load_color=RED if ram_used_gb is not None and ram_used_gb>26 else "#c8a243" if ram_used_gb is not None and ram_used_gb>17 else BLUE
            self.ram_used.config(text=self.fmt(ram_used_gb," GB",1),fg=AMBER); self.ram_load.config(text=self.fmt(d.get("ram_pct"),"%"),fg=ram_load_color)
            self.gpu_header.config(text=d.get("name",d.get("error","NVIDIA GPU")))
            self.vram.config(text=f"{self.fmt_vram(d.get('vram_used'))} / {self.fmt_vram(d.get('vram_total'))}")
            self.fan.config(text=self.fmt(d.get("fan"),"%"))
            self.ram_avail.config(text=self.fmt(d.get("ram_total")-d.get("ram_used") if d.get("ram_total") is not None else None," GB",1)); self.ram_total.config(text=self.fmt(d.get("ram_total")," GB",1))
            self.cpu_speed.config(text=self.fmt_ghz(d.get("cpu_freq")))
            estimate=self.estimate_system_power(d.get("power"),d.get("cpu"))
            self.power_estimate.config(text=f"{estimate:.0f} W / {self.psu_watts} W" if estimate is not None else f"— / {self.psu_watts} W")
            elapsed=int(time.monotonic()-self.started_at); hours,rem=divmod(elapsed,3600); minutes,seconds=divmod(rem,60)
            self.session_clock.config(text=f"{hours:02}:{minutes:02}:{seconds:02}")
            self.update_core_tiles(ct)
            if self.history_enabled:self.draw(hist)
        if self.running:self.root.after(350,self.refresh)
    def set_history_mode(self,mode):
        self.history_mode=mode
        for key,button in self.history_buttons.items():
            color=FG if key=="both" else BLUE if key=="gpu" else CYAN
            button.config(bg="#23314b" if key==mode else PANEL,fg=color if key==mode else MUTED)
        with self.lock:hist=list(self.samples)
        self.draw(hist)
    def set_history_range(self,value):
        self.history_range=value
        for key,button in self.history_range_buttons.items():
            button.config(bg="#23314b" if key==value else PANEL,fg=FG if key==value else MUTED)
        with self.lock:hist=list(self.samples)
        self.draw(hist)
    def draw(self,hist):
        if not self.history_enabled:return
        c=self.plot;c.delete("all");w=max(c.winfo_width(),200);h=max(c.winfo_height(),80);l=36;r=w-15;t=15;b=h-18
        for p in (0,50,100):
            y=b-(b-t)*p/100;c.create_line(l,y,r,y,fill="#263550");c.create_text(l-7,y,text=str(p),fill=MUTED,anchor="e",font=("Segoe UI",8))
        spans={"5m":300,"30m":1800,"1h":3600}
        span=spans.get(self.history_range)
        latest=hist[-1][0] if hist else 0
        cutoff=max(0,latest-span) if span is not None else 0
        if span is not None:hist=[sample for sample in hist if sample[0]>=cutoff]
        if len(hist)<2:return
        if span is None:span=max(latest,1); cutoff=0
        time_offset=max(0,span-latest) if self.history_range!="session" else 0
        series=((0,BLUE,"GPU"),(1,CYAN,"CPU"))
        if self.history_mode=="gpu":series=(series[0],)
        elif self.history_mode=="cpu":series=(series[1],)
        for idx,color,label in series:
            pts=[]
            for i,s in enumerate(hist):
                try:v=max(0,min(100,float(s[idx+1])))
                except (ValueError,TypeError):continue
                x_slot=s[0]-cutoff+time_offset
                pts.extend((l+(r-l)*x_slot/max(1,span),b-(b-t)*v/100))
            if len(pts)>3:
                c.create_line(*pts,fill=color,width=2,smooth=len(hist)<250)
                px,py=pts[-2:]; c.create_oval(px-3,py-3,px+3,py+3,fill=color,outline=PANEL,width=2)
        for n,(_,color,label) in enumerate(series):
            c.create_text(r-38*(len(series)-n),t-4,text=label,fill=color,font=("Segoe UI",9,"bold"))
    def close(self):
        self.running=False;self.poll_changed.set();self.core_reader.close();self.root.destroy()

if __name__=="__main__":
    root=tk.Tk()
    try: root.iconbitmap(os.path.join(os.path.dirname(os.path.abspath(__file__)),"Medic.ico"))
    except tk.TclError: pass
    App(root);root.mainloop()

