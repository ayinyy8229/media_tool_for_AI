"""
media_tool.py — 媒体批量处理工具
依赖: customtkinter, Pillow, pyinstaller
视频处理依赖系统安装的 ffmpeg (2-pass encoding)
"""

import os
import sys
import math
import shutil
import threading
import subprocess
import tempfile
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox
from PIL import Image

# ─── 常量 ────────────────────────────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".m4v"}
GIF_EXTS   = {".gif"}
OUTPUT_DIR_NAME = "配置资源"


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def find_ffmpeg() -> str:
    """查找 ffmpeg 路径：优先用 imageio_ffmpeg 自带的（已随 moviepy 打包），再找系统安装的"""
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    path = shutil.which("ffmpeg")
    if path:
        return path
    return ""


def get_video_duration(ffprobe: str, video_path: Path) -> float:
    """用 ffprobe 获取视频时长（秒），失败返回 0"""
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def process_image(src: Path, dst: Path, max_long_side: int, max_kb: int, log) -> bool:
    """
    处理单张图片：
    1. 等比缩放（长边不超过 max_long_side，不拉伸小图）
    2. 二分法逼近目标文件大小
    """
    try:
        img = Image.open(src)
        # 保留 EXIF（如有）
        exif = img.info.get("exif", b"")

        # 转 RGB（PNG/WEBP 可能有 alpha，JPEG 不支持）
        if img.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 等比缩放
        w, h = img.size
        long_side = max(w, h)
        if long_side > max_long_side:
            scale = max_long_side / long_side
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # 确定输出格式
        suffix = src.suffix.lower()
        if suffix in (".png",):
            # PNG 转 JPEG 以便控制大小
            out_path = dst.with_suffix(".jpg")
            fmt = "JPEG"
        elif suffix in (".webp",):
            out_path = dst
            fmt = "WEBP"
        else:
            out_path = dst
            fmt = "JPEG"

        target_bytes = max_kb * 1024
        lo, hi = 1, 95
        best_quality = hi
        best_data = None

        # 二分法逼近
        for _ in range(12):
            mid = (lo + hi) // 2
            import io
            buf = io.BytesIO()
            save_kwargs = {"format": fmt, "quality": mid, "optimize": True}
            if exif and fmt == "JPEG":
                save_kwargs["exif"] = exif
            img.save(buf, **save_kwargs)
            size = buf.tell()
            if size <= target_bytes:
                best_quality = mid
                best_data = buf.getvalue()
                lo = mid + 1
            else:
                hi = mid - 1
            if lo > hi:
                break

        if best_data is None:
            # 即使 quality=1 也超标，直接用最低质量
            buf = io.BytesIO()
            img.save(buf, format=fmt, quality=1, optimize=True)
            best_data = buf.getvalue()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(best_data)
        log(f"  [图片] {src.name} → {out_path.name}  ({len(best_data)//1024} KB, quality={best_quality})")
        return True
    except Exception as e:
        log(f"  [错误] {src.name}: {e}")
        return False


def process_video(ffmpeg_bin: str, src: Path, dst: Path, target_mb: float, log) -> bool:
    """
    2-pass encoding 压制视频到目标 MB 大小
    """
    ffprobe_bin = str(Path(ffmpeg_bin).parent / "ffprobe")
    if not Path(ffprobe_bin).exists():
        ffprobe_bin = shutil.which("ffprobe") or ffprobe_bin

    duration = get_video_duration(ffprobe_bin, src)
    if duration <= 0:
        log(f"  [错误] 无法获取视频时长: {src.name}")
        return False

    # 目标总比特率 (kbps)，预留 128kbps 给音频
    target_bits = target_mb * 8 * 1024 * 1024  # bits
    audio_bitrate_kbps = 128
    video_bitrate_kbps = max(100, int(target_bits / duration / 1000) - audio_bitrate_kbps)

    dst.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        passlog = str(Path(tmpdir) / "ffmpeg2pass")

        # Pass 1
        cmd_pass1 = [
            ffmpeg_bin, "-y",
            "-i", str(src),
            "-c:v", "libx264",
            "-b:v", f"{video_bitrate_kbps}k",
            "-pass", "1",
            "-passlogfile", passlog,
            "-an",
            "-f", "null",
            os.devnull,
        ]
        # Pass 2
        cmd_pass2 = [
            ffmpeg_bin, "-y",
            "-i", str(src),
            "-c:v", "libx264",
            "-b:v", f"{video_bitrate_kbps}k",
            "-pass", "2",
            "-passlogfile", passlog,
            "-c:a", "aac",
            "-b:a", f"{audio_bitrate_kbps}k",
            "-movflags", "+faststart",
            str(dst),
        ]

        log(f"  [视频] {src.name} 时长={duration:.1f}s 目标码率={video_bitrate_kbps}kbps")
        log(f"         Pass 1 ...")
        try:
            r1 = subprocess.run(cmd_pass1, capture_output=True, text=True, timeout=600)
            if r1.returncode != 0:
                log(f"  [错误] Pass1 失败: {r1.stderr[-300:]}")
                return False
            log(f"         Pass 2 ...")
            r2 = subprocess.run(cmd_pass2, capture_output=True, text=True, timeout=600)
            if r2.returncode != 0:
                log(f"  [错误] Pass2 失败: {r2.stderr[-300:]}")
                return False
            actual_mb = dst.stat().st_size / 1024 / 1024
            log(f"  [视频] {src.name} → {dst.name}  ({actual_mb:.2f} MB)")
            return True
        except subprocess.TimeoutExpired:
            log(f"  [错误] 处理超时: {src.name}")
            return False
        except Exception as e:
            log(f"  [错误] {src.name}: {e}")
            return False


def convert_mp4_to_gif(src: Path, dst: Path, start_time: float, end_time, log) -> bool:
    """
    用 ffmpeg 将 MP4 片段转为 GIF。
    - 高度固定 320px，宽度等比缩放（scale=-1:320）
    - 帧率 10 FPS，palette 优化画质
    """
    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        log(f"  [错误] {src.name}: 未找到 ffmpeg")
        return False

    end_str = f"{end_time}s" if end_time is not None else "结尾"
    log(f"  [MP4→GIF] {src.name}  截取 {start_time}s ~ {end_str} ...")

    dst.parent.mkdir(parents=True, exist_ok=True)

    time_args = ["-ss", str(start_time)]
    if end_time is not None:
        time_args += ["-t", str(end_time - start_time)]

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            palette = str(Path(tmpdir) / "palette.png")

            # Pass 1：生成调色板
            cmd_pal = [
                ffmpeg_bin, "-y", *time_args, "-i", str(src),
                "-vf", "fps=10,scale=-1:320:flags=lanczos,palettegen",
                palette,
            ]
            r1 = subprocess.run(cmd_pal, capture_output=True, timeout=120)
            if r1.returncode != 0:
                log(f"  [错误] {src.name}: 生成调色板失败\n{r1.stderr.decode(errors='ignore')[-200:]}")
                return False

            # Pass 2：合成 GIF
            cmd_gif = [
                ffmpeg_bin, "-y", *time_args, "-i", str(src), "-i", palette,
                "-lavfi", "fps=10,scale=-1:320:flags=lanczos [x]; [x][1:v] paletteuse",
                str(dst),
            ]
            r2 = subprocess.run(cmd_gif, capture_output=True, timeout=300)
            if r2.returncode != 0:
                log(f"  [错误] {src.name}: GIF 合成失败\n{r2.stderr.decode(errors='ignore')[-200:]}")
                return False

        actual_kb = dst.stat().st_size / 1024
        log(f"  [MP4→GIF] {src.name} → {dst.name}  ({actual_kb:.0f} KB)")
        return True
    except Exception as e:
        log(f"  [错误] {src.name}: {e}")
        return False


def process_gif(ffmpeg_bin: str, src: Path, dst: Path, target_kb: float, log) -> bool:
    """
    用 ffmpeg 压缩 GIF，策略：
    1. 先用优化调色板（256色）压一次
    2. 若仍超标，逐步降低色彩数（128→64→32）
    3. 若仍超标，等比缩放分辨率（0.75→0.5→0.35）
    4. 若仍超标，降低帧率（15→10→6）
    每步都检查大小，达标即停止。
    """
    target_bytes = int(target_kb * 1024)
    dst.parent.mkdir(parents=True, exist_ok=True)

    def run_gif(extra_vf: str, fps: int, colors: int, scale: float, out: Path) -> bool:
        """构建 ffmpeg 命令并执行，返回是否成功"""
        with tempfile.TemporaryDirectory() as tmpdir:
            palette = str(Path(tmpdir) / "palette.png")
            # 构建 vf 滤镜链
            vf_parts = []
            if scale < 1.0:
                vf_parts.append(f"scale=iw*{scale}:ih*{scale}:flags=lanczos")
            if fps > 0:
                vf_parts.append(f"fps={fps}")
            vf_base = ",".join(vf_parts) if vf_parts else "null"

            # Pass 1: 生成调色板
            cmd_pal = [
                ffmpeg_bin, "-y", "-i", str(src),
                "-vf", f"{vf_base},palettegen=max_colors={colors}:stats_mode=diff",
                palette,
            ]
            r = subprocess.run(cmd_pal, capture_output=True, timeout=120)
            if r.returncode != 0:
                return False

            # Pass 2: 用调色板合成 GIF
            cmd_gif = [
                ffmpeg_bin, "-y",
                "-i", str(src),
                "-i", palette,
                "-lavfi", f"{vf_base} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                str(out),
            ]
            r2 = subprocess.run(cmd_gif, capture_output=True, timeout=300)
            return r2.returncode == 0

    # 压缩策略矩阵：(colors, scale, fps)
    strategies = [
        (256, 1.0,  0),   # 仅优化调色板
        (128, 1.0,  0),   # 减少色彩
        (64,  1.0,  0),
        (32,  1.0,  0),
        (256, 0.75, 0),   # 缩小分辨率
        (128, 0.75, 0),
        (256, 0.5,  0),
        (128, 0.5,  0),
        (64,  0.5,  0),
        (256, 0.5,  15),  # 降帧率
        (128, 0.5,  10),
        (64,  0.35, 10),
        (32,  0.35, 6),
    ]

    orig_kb = src.stat().st_size / 1024
    log(f"  [GIF] {src.name}  原始={orig_kb:.0f} KB  目标≤{target_kb:.0f} KB")

    for colors, scale, fps in strategies:
        desc = f"colors={colors} scale={scale}"
        if fps:
            desc += f" fps={fps}"
        log(f"         尝试 {desc} ...")
        if run_gif("", fps, colors, scale, dst):
            actual_kb = dst.stat().st_size / 1024
            if actual_kb <= target_kb:
                log(f"  [GIF] {src.name} → {dst.name}  ({actual_kb:.0f} KB)  ✓")
                return True
        else:
            log(f"         ffmpeg 执行失败，跳过")

    # 所有策略都试完，保留最后一次结果
    if dst.exists():
        actual_kb = dst.stat().st_size / 1024
        log(f"  [GIF] {src.name} → {dst.name}  ({actual_kb:.0f} KB)  (已尽力压缩)")
        return True

    log(f"  [错误] {src.name}: GIF 压缩全部失败")
    return False


# ─── GUI ─────────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("媒体批量处理工具")
        self.geometry("720x620")
        self.resizable(True, True)
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self._build_ui()
        self._toggle_image_section()
        self._toggle_video_section()
        self._toggle_gif_section()
        self._toggle_mp4gif_section()

    def _build_ui(self):
        pad = {"padx": 16, "pady": 6}

        # ── 文件夹选择 ──
        folder_frame = ctk.CTkFrame(self)
        folder_frame.pack(fill="x", **pad)

        ctk.CTkLabel(folder_frame, text="目标文件夹：", width=100).pack(side="left", padx=(8, 4), pady=8)
        self.folder_var = ctk.StringVar()
        ctk.CTkEntry(folder_frame, textvariable=self.folder_var, width=420).pack(side="left", padx=4, pady=8)
        ctk.CTkButton(folder_frame, text="浏览", width=70, command=self._browse).pack(side="left", padx=(4, 8), pady=8)

        # ── 图片区 ──
        self.img_check_var = ctk.BooleanVar(value=True)
        img_toggle = ctk.CTkCheckBox(self, text="处理图片", variable=self.img_check_var,
                                     command=self._toggle_image_section)
        img_toggle.pack(anchor="w", padx=20, pady=(10, 2))

        self.img_frame = ctk.CTkFrame(self)
        self.img_frame.pack(fill="x", padx=20, pady=(0, 6))

        ctk.CTkLabel(self.img_frame, text="最大长边 (px)：").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        self.img_long_side = ctk.CTkEntry(self.img_frame, width=120, placeholder_text="如 1920")
        self.img_long_side.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        self.img_long_side.insert(0, "1920")

        ctk.CTkLabel(self.img_frame, text="目标最大大小 (KB)：").grid(row=0, column=2, padx=8, pady=8, sticky="e")
        self.img_max_kb = ctk.CTkEntry(self.img_frame, width=120, placeholder_text="如 500")
        self.img_max_kb.grid(row=0, column=3, padx=8, pady=8, sticky="w")
        self.img_max_kb.insert(0, "500")

        # ── 视频区 ──
        self.vid_check_var = ctk.BooleanVar(value=False)
        vid_toggle = ctk.CTkCheckBox(self, text="处理视频", variable=self.vid_check_var,
                                     command=self._toggle_video_section)
        vid_toggle.pack(anchor="w", padx=20, pady=(6, 2))

        self.vid_frame = ctk.CTkFrame(self)
        self.vid_frame.pack(fill="x", padx=20, pady=(0, 6))

        ctk.CTkLabel(self.vid_frame, text="目标文件大小 (MB)：").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        self.vid_target_mb = ctk.CTkEntry(self.vid_frame, width=120, placeholder_text="如 50")
        self.vid_target_mb.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        self.vid_target_mb.insert(0, "50")

        # ── GIF 区 ──
        self.gif_check_var = ctk.BooleanVar(value=False)
        gif_toggle = ctk.CTkCheckBox(self, text="处理 GIF", variable=self.gif_check_var,
                                     command=self._toggle_gif_section)
        gif_toggle.pack(anchor="w", padx=20, pady=(6, 2))

        self.gif_frame = ctk.CTkFrame(self)
        self.gif_frame.pack(fill="x", padx=20, pady=(0, 6))

        ctk.CTkLabel(self.gif_frame, text="目标最大大小 (MB)：").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        self.gif_target = ctk.CTkEntry(self.gif_frame, width=120, placeholder_text="如 10")
        self.gif_target.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        self.gif_target.insert(0, "10")
        ctk.CTkLabel(self.gif_frame, text="(单位：MB，如 10 表示 10 MB)",
                     text_color="gray").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        # ── MP4 转 GIF 区 ──
        self.mp4gif_check_var = ctk.BooleanVar(value=False)
        mp4gif_toggle = ctk.CTkCheckBox(self, text="MP4 转 GIF", variable=self.mp4gif_check_var,
                                        command=self._toggle_mp4gif_section)
        mp4gif_toggle.pack(anchor="w", padx=20, pady=(6, 2))

        self.mp4gif_frame = ctk.CTkFrame(self)
        self.mp4gif_frame.pack(fill="x", padx=20, pady=(0, 6))

        ctk.CTkLabel(self.mp4gif_frame, text="开始时间 (秒)：").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        self.mp4gif_start = ctk.CTkEntry(self.mp4gif_frame, width=100, placeholder_text="如 0")
        self.mp4gif_start.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        self.mp4gif_start.insert(0, "0")

        ctk.CTkLabel(self.mp4gif_frame, text="结束时间 (秒)：").grid(row=0, column=2, padx=8, pady=8, sticky="e")
        self.mp4gif_end = ctk.CTkEntry(self.mp4gif_frame, width=100, placeholder_text="留空=到结尾")
        self.mp4gif_end.grid(row=0, column=3, padx=8, pady=8, sticky="w")

        ctk.CTkLabel(self.mp4gif_frame, text="留空表示到结尾",
                     text_color="gray").grid(row=0, column=4, padx=8, pady=8, sticky="w")

        self.start_btn = ctk.CTkButton(self, text="开始处理", height=44,
                                       font=ctk.CTkFont(size=16, weight="bold"),
                                       command=self._start)
        self.start_btn.pack(fill="x", padx=20, pady=10)

        # ── 日志框 ──
        ctk.CTkLabel(self, text="处理日志：", anchor="w").pack(fill="x", padx=20)
        self.log_box = ctk.CTkTextbox(self, height=220, font=ctk.CTkFont(family="Courier", size=12))
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def _toggle_image_section(self):
        state = "normal" if self.img_check_var.get() else "disabled"
        for w in self.img_frame.winfo_children():
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _toggle_video_section(self):
        state = "normal" if self.vid_check_var.get() else "disabled"
        for w in self.vid_frame.winfo_children():
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _toggle_gif_section(self):
        state = "normal" if self.gif_check_var.get() else "disabled"
        for w in self.gif_frame.winfo_children():
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _toggle_mp4gif_section(self):
        state = "normal" if self.mp4gif_check_var.get() else "disabled"
        for w in self.mp4gif_frame.winfo_children():
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _browse(self):
        folder = filedialog.askdirectory(title="选择目标文件夹")
        if folder:
            self.folder_var.set(folder)

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.update_idletasks()

    def _start(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("错误", "请选择有效的文件夹")
            return
        if not self.img_check_var.get() and not self.vid_check_var.get() \
                and not self.gif_check_var.get() and not self.mp4gif_check_var.get():
            messagebox.showerror("错误", "请至少勾选一种处理类型")
            return

        self.start_btn.configure(state="disabled", text="处理中...")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        folder = Path(self.folder_var.get().strip())
        output_dir = folder.parent / OUTPUT_DIR_NAME
        output_dir.mkdir(parents=True, exist_ok=True)
        self._log(f"输出目录: {output_dir}")

        ok = err = 0

        # ── 图片处理 ──
        if self.img_check_var.get():
            try:
                max_side = int(self.img_long_side.get())
                max_kb = int(self.img_max_kb.get())
            except ValueError:
                self._log("[错误] 图片参数无效，请输入整数")
                self._done()
                return

            images = [f for f in folder.iterdir() if f.suffix.lower() in IMAGE_EXTS]
            self._log(f"\n找到 {len(images)} 张图片，开始处理...")
            for src in images:
                dst = output_dir / src.name
                if process_image(src, dst, max_side, max_kb, self._log):
                    ok += 1
                else:
                    err += 1

        # ── 视频处理 ──
        if self.vid_check_var.get():
            ffmpeg_bin = find_ffmpeg()
            if not ffmpeg_bin:
                self._log("\n[错误] 未找到 ffmpeg，请联系开发者或重新下载工具")
                self._done()
                return

            try:
                target_mb = float(self.vid_target_mb.get())
            except ValueError:
                self._log("[错误] 视频目标大小参数无效")
                self._done()
                return

            videos = [f for f in folder.iterdir() if f.suffix.lower() in VIDEO_EXTS]
            self._log(f"\n找到 {len(videos)} 个视频，开始处理...")
            for src in videos:
                dst = output_dir / (src.stem + ".mp4")
                if process_video(ffmpeg_bin, src, dst, target_mb, self._log):
                    ok += 1
                else:
                    err += 1

        # ── GIF 处理 ──
        if self.gif_check_var.get():
            ffmpeg_bin = find_ffmpeg()
            if not ffmpeg_bin:
                self._log("\n[错误] 未找到 ffmpeg，请联系开发者或重新下载工具")
                self._done()
                return

            try:
                gif_target_kb = float(self.gif_target.get().strip()) * 1024
            except ValueError:
                self._log("[错误] GIF 目标大小参数无效，请输入数字（MB）")
                self._done()
                return

            gifs = [f for f in folder.iterdir() if f.suffix.lower() in GIF_EXTS]
            self._log(f"\n找到 {len(gifs)} 个 GIF，开始处理...")
            for src in gifs:
                dst = output_dir / src.name
                if process_gif(ffmpeg_bin, src, dst, gif_target_kb, self._log):
                    ok += 1
                else:
                    err += 1

        # ── MP4 转 GIF ──
        if self.mp4gif_check_var.get():
            try:
                start_t = float(self.mp4gif_start.get().strip() or "0")
            except ValueError:
                self._log("[错误] 开始时间参数无效，请输入数字（秒）")
                self._done()
                return
            end_raw = self.mp4gif_end.get().strip()
            end_t = float(end_raw) if end_raw else None

            mp4s = [f for f in folder.iterdir() if f.suffix.lower() == ".mp4"]
            self._log(f"\n找到 {len(mp4s)} 个 MP4 文件，开始转换 GIF...")
            for src in mp4s:
                dst = output_dir / (src.stem + ".gif")
                if convert_mp4_to_gif(src, dst, start_t, end_t, self._log):
                    ok += 1
                else:
                    err += 1

        self._log(f"\n完成！成功 {ok} 个，失败 {err} 个。")
        self._log(f"输出目录: {output_dir}")
        self._done()

    def _done(self):
        self.start_btn.configure(state="normal", text="开始处理")


# ─── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
