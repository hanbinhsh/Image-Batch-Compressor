import sys
import os
import multiprocessing
import threading
from datetime import datetime
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed

# 强制 qfluentwidgets 使用 PySide6
os.environ["QT_API"] = "pyside6" 
Image.MAX_IMAGE_PIXELS = None

# 兼容旧版本 Pillow 的重采样常量
RESAMPLING_LANCZOS = getattr(Image, 'Resampling', Image).LANCZOS

from PySide6.QtCore import Qt, QThread, Signal, QDate
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFileDialog, QTabWidget
from qfluentwidgets import (PushButton, LineEdit, CheckBox, ComboBox, 
                            CalendarPicker, ProgressBar, MessageBox, 
                            StrongBodyLabel, BodyLabel, setTheme, Theme, 
                            SpinBox, TextEdit)

def format_size(size_in_bytes):
    """将字节转换为易读的容量单位"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"

def compress_single_image(filepath, quality, keep_format, resize_mode, resize_png_only, pause_event, stop_event):
    """
    独立的压缩函数
    注意：大小阈值判断已移至扫描阶段，此处传入的文件均为需处理文件
    """
    try:
        if stop_event.is_set():
            return False, 0, 0, "已停止", filepath
            
        pause_event.wait()
        if stop_event.is_set():
            return False, 0, 0, "已停止", filepath

        original_size = os.path.getsize(filepath)
        img = Image.open(filepath)
        target_path = filepath
        is_png = filepath.lower().endswith('.png')

        # ==== 1. 处理尺寸缩放 (1/2, 1/4, 1/8) ====
        should_resize = True
        if resize_png_only and not is_png:
            should_resize = False

        if should_resize and resize_mode > 0:
            scale = 1
            if resize_mode == 1: scale = 0.5   # 1/2
            elif resize_mode == 2: scale = 0.25  # 1/4
            elif resize_mode == 3: scale = 0.125 # 1/8
            
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            img = img.resize((new_w, new_h), RESAMPLING_LANCZOS)

        # ==== 2. 格式转换预处理 ====
        if not keep_format and is_png:
            target_path = os.path.splitext(filepath)[0] + '.jpg'
        
        # 处理调色板模式
        if img.mode == 'P':
            img = img.convert('RGBA')
            
        # 处理透明度转 JPG 的背景问题
        if img.mode in ('RGBA', 'LA') and target_path.lower().endswith(('.jpg', '.jpeg')):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != 'RGB' and target_path.lower().endswith(('.jpg', '.jpeg')):
            img = img.convert('RGB')
            
        # ==== 3. 保存逻辑 ====
        if target_path.lower().endswith('.png'):
            img.save(target_path, optimize=True)
        else:
            img.save(target_path, optimize=True, quality=quality)
        
        # 如果转换了格式，删除原文件
        if target_path != filepath and os.path.exists(target_path):
            os.remove(filepath)
            
        new_size = os.path.getsize(target_path)
        return True, original_size, new_size, "", target_path
    except Exception as e:
        return False, 0, 0, str(e), filepath


class ImageScanThread(QThread):
    """
    扫描线程：负责筛选文件
    finished_scan 信号返回: (待处理列表, 因日期跳过数, 因大小跳过数, 待处理文件总大小Bytes)
    """
    scan_progress = Signal(int, int) 
    finished_scan = Signal(list, int, int, object) 

    def __init__(self, folder, exts, cutoff_timestamp, threshold_kb):
        super().__init__()
        self.folder = folder
        self.exts = tuple(exts)
        self.cutoff = cutoff_timestamp
        self.min_size = threshold_kb * 1024 # 转换为字节
        self.is_stopped = False

    def stop(self): self.is_stopped = True

    def run(self):
        target_files = []
        skipped_by_date = 0
        skipped_by_size = 0
        total_scanned_size = 0 # 待处理文件的总大小
        scanned_count = 0
        dirs_to_process = [self.folder]

        while dirs_to_process:
            if self.is_stopped: break
            current_dir = dirs_to_process.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if self.is_stopped: break
                        if entry.is_dir(follow_symlinks=False):
                            dirs_to_process.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            scanned_count += 1
                            if entry.name.lower().endswith(self.exts):
                                stat = entry.stat()
                                # 1. 检查日期
                                if stat.st_mtime >= self.cutoff:
                                    skipped_by_date += 1
                                    continue
                                
                                # 2. 检查大小
                                if stat.st_size < self.min_size:
                                    skipped_by_size += 1
                                    continue

                                # 3. 符合条件
                                target_files.append(entry.path)
                                total_scanned_size += stat.st_size

                            if scanned_count % 1000 == 0:
                                self.scan_progress.emit(scanned_count, len(target_files))
            except PermissionError: continue

        self.finished_scan.emit(target_files, skipped_by_date, skipped_by_size, total_scanned_size)


class CompressThread(QThread):
    progress_update = Signal(int, int) 
    finished_work = Signal(int, int, object, object) 

    def __init__(self, target_files, quality, keep_format, resize_mode, resize_png_only):
        super().__init__()
        self.target_files = target_files
        self.quality = quality
        self.keep_format = keep_format
        self.resize_mode = resize_mode
        self.resize_png_only = resize_png_only
        
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()

    def pause(self): self.pause_event.clear()
    def resume(self): self.pause_event.set()
    def stop(self):
        self.stop_event.set()
        self.pause_event.set()

    def run(self):
        total = len(self.target_files)
        success_count = fail_count = total_original_size = total_new_size = 0
        
        if total == 0 or self.stop_event.is_set():
            self.finished_work.emit(0, 0, 0, 0)
            return

        max_workers = max(1, multiprocessing.cpu_count() - 1)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {
                executor.submit(
                    compress_single_image, 
                    path, 
                    self.quality, 
                    self.keep_format, 
                    self.resize_mode, 
                    self.resize_png_only, 
                    self.pause_event, 
                    self.stop_event
                ): path 
                for path in self.target_files
            }
            
            completed = 0
            for future in as_completed(future_to_path):
                success, orig_size, new_size, error_msg, filepath = future.result()
                
                if success:
                    success_count += 1
                    total_original_size += orig_size
                    total_new_size += new_size
                else:
                    if error_msg != "已停止":
                        fail_count += 1
                        print(f"[压缩失败] {filepath} - 原因: {error_msg}")
                
                completed += 1
                if (completed % 100 == 0 or completed == total) and not self.stop_event.is_set():
                    self.progress_update.emit(completed, total)

        self.finished_work.emit(success_count, fail_count, total_original_size, total_new_size)

class ScanExtensionsThread(QThread):
    finished_scan = Signal(dict)
    scan_progress = Signal(int)
    def __init__(self, folder):
        super().__init__()
        self.folder = folder
    def run(self):
        ext_counts = {}
        scanned_count = 0
        dirs_to_process =[self.folder]
        while dirs_to_process:
            current_dir = dirs_to_process.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False): dirs_to_process.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            scanned_count += 1
                            ext = os.path.splitext(entry.name)[1].lower()
                            if not ext: ext = "[无后缀名文件]"
                            ext_counts[ext] = ext_counts.get(ext, 0) + 1
                            if scanned_count % 1000 == 0: self.scan_progress.emit(scanned_count)
            except PermissionError: continue
        self.finished_scan.emit(ext_counts)

class CompressApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("图片缓存深度压缩工具")
        self.resize(700, 650)
        setTheme(Theme.AUTO)

        self.current_state = "INIT"
        self.target_files =[]
        # 统计数据
        self.skipped_by_date = 0
        self.skipped_by_size = 0
        self.scan_total_size = 0

        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # 顶部文件夹选择
        folder_layout = QHBoxLayout()
        self.folder_input = LineEdit()
        self.folder_input.setPlaceholderText("请选择文件夹路径...")
        self.folder_input.setReadOnly(True)
        self.btn_browse = PushButton("选择文件夹")
        self.btn_browse.clicked.connect(self.browse_folder)
        folder_layout.addWidget(self.folder_input)
        folder_layout.addWidget(self.btn_browse)
        main_layout.addLayout(folder_layout)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.tab_compress = QWidget()
        self.setup_compress_tab()
        self.tabs.addTab(self.tab_compress, "压缩工具")

        self.tab_debug = QWidget()
        self.setup_debug_tab()
        self.tabs.addTab(self.tab_debug, "调试工具")

    def setup_compress_tab(self):
        layout = QVBoxLayout(self.tab_compress)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        # 1. 格式选项
        type_layout = QHBoxLayout()
        type_layout.addWidget(StrongBodyLabel("处理格式:"))
        self.cb_jpg = CheckBox(".jpg / .jpeg")
        self.cb_jpg.setChecked(True)
        self.cb_png = CheckBox(".png")
        self.cb_png.setChecked(False)
        self.cb_keep_format = CheckBox("保留原图格式")
        self.cb_keep_format.setChecked(True)
        
        type_layout.addWidget(self.cb_jpg)
        type_layout.addWidget(self.cb_png)
        type_layout.addSpacing(20)
        type_layout.addWidget(self.cb_keep_format)
        type_layout.addStretch(1)
        layout.addLayout(type_layout)

        # 2. JPG 压缩质量
        quality_layout = QHBoxLayout()
        quality_layout.addWidget(StrongBodyLabel("JPG质量:"))
        self.combo_quality = ComboBox()
        self.combo_quality.addItems([
            "低质量 (40%) - 极限省流", 
            "中等质量 (60%) - 推荐", 
            "高质量 (80%) - 视觉无损",
            "自定义..."
        ])
        self.combo_quality.setCurrentIndex(1)
        
        self.spin_custom_quality = SpinBox()
        self.spin_custom_quality.setRange(1, 100)
        self.spin_custom_quality.setValue(70)
        self.spin_custom_quality.setFixedWidth(180)
        self.spin_custom_quality.hide()
        
        self.combo_quality.currentIndexChanged.connect(self.on_quality_changed)
        quality_layout.addWidget(self.combo_quality)
        quality_layout.addWidget(self.spin_custom_quality)
        
        lbl_hint = BodyLabel("(仅对 JPG 生效，PNG 不适用此选项)")
        lbl_hint.setStyleSheet("color: gray;")
        quality_layout.addWidget(lbl_hint)
        quality_layout.addStretch(1)
        layout.addLayout(quality_layout)

        # 3. 尺寸调整
        resize_layout = QHBoxLayout()
        resize_layout.addWidget(StrongBodyLabel("尺寸调整:"))
        self.combo_resize = ComboBox()
        self.combo_resize.addItems([
            "保持原尺寸 (不缩放)",
            "缩放至原图的 1/2",
            "缩放至原图的 1/4",
            "缩放至原图的 1/8"
        ])
        self.combo_resize.setCurrentIndex(0)
        
        self.cb_resize_png_only = CheckBox("仅对 PNG 图片应用缩放")
        self.cb_resize_png_only.setChecked(False)
        self.cb_resize_png_only.setToolTip("勾选后，JPG 图片将保持原分辨率，只有 PNG 会被缩小")

        resize_layout.addWidget(self.combo_resize)
        resize_layout.addSpacing(10)
        resize_layout.addWidget(self.cb_resize_png_only)
        resize_layout.addStretch(1)
        layout.addLayout(resize_layout)

        # 4. 阈值设置
        threshold_layout = QHBoxLayout()
        threshold_layout.addWidget(StrongBodyLabel("忽略小图:"))
        self.spin_threshold = SpinBox()
        self.spin_threshold.setRange(0, 10240) # 0 - 10MB
        self.spin_threshold.setValue(50) # 默认 50KB
        self.spin_threshold.setFixedWidth(180)
        
        threshold_layout.addWidget(self.spin_threshold)
        threshold_layout.addWidget(BodyLabel("KB (小于此大小的文件将不进行压缩/缩放)"))
        threshold_layout.addStretch(1)
        layout.addLayout(threshold_layout)

        # 5. 时间筛选
        date_layout = QHBoxLayout()
        date_layout.addWidget(StrongBodyLabel("时间筛选:"))
        self.combo_date = ComboBox()
        self.combo_date.addItems(["1个月前", "3个月前", "6个月前", "1年前", "3年前", "自定义日期..."])
        self.combo_date.setCurrentIndex(2)
        self.combo_date.currentIndexChanged.connect(self.on_date_changed)
        
        self.date_picker = CalendarPicker()
        self.date_picker.setDate(QDate.currentDate().addMonths(-6))
        self.date_picker.hide()
        
        date_layout.addWidget(self.combo_date)
        date_layout.addWidget(self.date_picker)
        date_layout.addStretch(1)
        layout.addLayout(date_layout)

        # 信号绑定 - 任何筛选条件变动都需要重新扫描
        self.cb_jpg.stateChanged.connect(self.on_scan_settings_changed)
        self.cb_png.stateChanged.connect(self.on_scan_settings_changed)
        self.combo_date.currentIndexChanged.connect(self.on_scan_settings_changed)
        self.date_picker.dateChanged.connect(self.on_scan_settings_changed)
        self.spin_threshold.valueChanged.connect(self.on_scan_settings_changed) # 绑定阈值

        # 进度与状态
        self.progress_bar = ProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.lbl_status = BodyLabel("")
        layout.addWidget(self.lbl_status)

        # 按钮组
        btn_layout = QHBoxLayout()
        self.btn_action = PushButton("1. 扫描图片")
        self.btn_action.clicked.connect(self.on_action_clicked)
        
        self.btn_pause = PushButton("暂停")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.hide()

        self.btn_stop = PushButton("停止")
        self.btn_stop.clicked.connect(self.stop_process)
        self.btn_stop.hide()

        btn_layout.addWidget(self.btn_action)
        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(self.btn_stop)
        layout.addLayout(btn_layout)

    def setup_debug_tab(self):
        layout = QVBoxLayout(self.tab_debug)
        self.btn_scan_exts = PushButton("扫描文件夹内所有存在的文件格式")
        self.btn_scan_exts.clicked.connect(self.start_debug_scan)
        layout.addWidget(self.btn_scan_exts)
        self.text_debug_output = TextEdit()
        self.text_debug_output.setReadOnly(True)
        layout.addWidget(self.text_debug_output)

    def update_ui_state(self, state):
        self.current_state = state
        if state == "INIT":
            self.btn_action.setText("1. 扫描图片")
            self.btn_action.setEnabled(True)
            self.btn_pause.hide()
            self.btn_stop.hide()
            self.progress_bar.hide()
            self.tabs.setTabEnabled(1, True)
            self.folder_input.setEnabled(True)
            self.btn_browse.setEnabled(True)
        elif state == "SCANNING":
            self.btn_action.setText("扫描中...")
            self.btn_action.setEnabled(False)
            self.btn_pause.hide()
            self.btn_stop.show()
            self.btn_stop.setText("停止扫描")
            self.btn_stop.setEnabled(True)
            self.progress_bar.setRange(0, 0)
            self.progress_bar.show()
            self.tabs.setTabEnabled(1, False)
        elif state == "READY":
            self.btn_action.setText("2. 开始压缩")
            self.btn_action.setEnabled(True)
            self.btn_pause.hide()
            self.btn_stop.hide()
            self.progress_bar.hide()
        elif state == "COMPRESSING":
            self.btn_action.setText("压缩中...")
            self.btn_action.setEnabled(False)
            self.btn_pause.show()
            self.btn_pause.setText("暂停")
            self.btn_pause.setEnabled(True)
            self.btn_stop.show()
            self.btn_stop.setText("停止")
            self.btn_stop.setEnabled(True)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.show()
            self.tabs.setTabEnabled(1, False)
        elif state == "PAUSED":
            self.btn_pause.setText("继续")
            self.lbl_status.setText("已暂停压缩。")

    def on_scan_settings_changed(self):
        if self.current_state == "READY":
            self.target_files =[]
            self.update_ui_state("INIT")
            self.lbl_status.setText("筛选条件(日期/格式/大小)已更改，请重新扫描文件目录。")

    def on_quality_changed(self, index):
        if index == 3: self.spin_custom_quality.show()
        else: self.spin_custom_quality.hide()

    def on_date_changed(self, index):
        if index == 5: self.date_picker.show()
        else: self.date_picker.hide()

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self.folder_input.setText(folder)
            self.update_ui_state("INIT")
            self.lbl_status.setText("")

    def on_action_clicked(self):
        if self.current_state == "INIT": self.start_scan()
        elif self.current_state == "READY": self.start_compression()

    def start_scan(self):
        folder = self.folder_input.text()
        if not folder or not os.path.exists(folder):
            MessageBox("错误", "请先选择一个有效的文件夹路径！", self).exec()
            return

        exts = []
        if self.cb_jpg.isChecked(): exts.extend(['.jpg', '.jpeg'])
        if self.cb_png.isChecked(): exts.append('.png')
        if not exts:
            MessageBox("错误", "请至少选择一种需要处理的文件格式！", self).exec()
            return
            
        date_index = self.combo_date.currentIndex()
        if date_index == 5:
            selected_date = self.date_picker.getDate()
            dt = datetime(selected_date.year(), selected_date.month(), selected_date.day())
        else:
            current_date = QDate.currentDate()
            if date_index == 0: target_date = current_date.addMonths(-1)
            elif date_index == 1: target_date = current_date.addMonths(-3)
            elif date_index == 2: target_date = current_date.addMonths(-6)
            elif date_index == 3: target_date = current_date.addMonths(-12)
            elif date_index == 4: target_date = current_date.addMonths(-36)
            dt = datetime(target_date.year(), target_date.month(), target_date.day())
            
        cutoff_timestamp = dt.timestamp()
        
        # 获取阈值
        threshold_kb = self.spin_threshold.value()

        self.update_ui_state("SCANNING")
        self.lbl_status.setText("🚀 正在极速扫描目录中，请稍候...")

        self.scan_thread = ImageScanThread(folder, exts, cutoff_timestamp, threshold_kb)
        self.scan_thread.scan_progress.connect(self.update_scan_progress)
        self.scan_thread.finished_scan.connect(self.on_scan_finished)
        self.scan_thread.start()

    def update_scan_progress(self, scanned, found):
        self.lbl_status.setText(f"🔍 扫描中... 已检查文件: {scanned} 个 | 发现待处理: {found} 张")

    def on_scan_finished(self, target_files, skipped_by_date, skipped_by_size, total_size):
        if self.current_state != "SCANNING": return
        self.target_files = target_files
        self.skipped_by_date = skipped_by_date
        self.skipped_by_size = skipped_by_size
        self.scan_total_size = total_size
        
        if len(target_files) == 0:
            self.update_ui_state("INIT")
            msg = (f"扫描完毕，未发现符合条件的图片。\n"
                   f"• 因日期较新跳过: {skipped_by_date} 张\n"
                   f"• 因小于阈值跳过: {skipped_by_size} 张")
            self.lbl_status.setText(f"扫描完毕。无待处理文件。 (跳过: 日期 {skipped_by_date} / 大小 {skipped_by_size})")
            MessageBox("未发现文件", msg, self).exec()
        else:
            self.update_ui_state("READY")
            self.lbl_status.setText(f"✅ 扫描完成！共 {len(target_files)} 张图片 ({format_size(total_size)}) 等待压缩。")
            
            # 扫描完成后的弹窗信息
            msg = (f"扫描完成！\n"
                   f"──────────────────\n"
                   f"📂 待处理文件: {len(target_files)} 张\n"
                   f"💾 总占用大小: {format_size(total_size)}\n"
                   f"──────────────────\n"
                   f"🚫 已忽略文件:\n"
                   f"• 因日期较新: {skipped_by_date} 张\n"
                   f"• 因小于阈值: {skipped_by_size} 张")
            MessageBox("扫描结果", msg, self).exec()

    def start_compression(self):
        w = MessageBox(
            "高危操作确认",
            f"⚠️ 警告：\n图片压缩将直接覆盖原文件且无法恢复！\n即将并发处理 {len(self.target_files)} 张图片，您确定要继续吗？",
            self
        )
        if not w.exec(): return 

        quality_index = self.combo_quality.currentIndex()
        if quality_index == 3: quality = self.spin_custom_quality.value()
        else: quality = {0: 40, 1: 60, 2: 80}[quality_index]
            
        keep_format = self.cb_keep_format.isChecked()
        resize_mode = self.combo_resize.currentIndex()
        resize_png_only = self.cb_resize_png_only.isChecked()
        
        # 阈值已在扫描阶段使用，不需要传给压缩线程

        self.update_ui_state("COMPRESSING")
        self.progress_bar.setValue(0)
        self.lbl_status.setText("正在分配线程...")

        self.compress_thread = CompressThread(
            self.target_files, 
            quality, 
            keep_format, 
            resize_mode, 
            resize_png_only
        )
        self.compress_thread.progress_update.connect(self.update_compress_progress)
        self.compress_thread.finished_work.connect(self.compression_finished)
        self.compress_thread.start()

    def update_compress_progress(self, current, total):
        self.lbl_status.setText(f"⚙️ 并发压缩中: {current} / {total} (占用 CPU 多核)")
        percent = int((current / total) * 100)
        self.progress_bar.setValue(percent)

    def compression_finished(self, success, fail, orig_size, new_size):
        self.target_files =[] 
        self.update_ui_state("INIT")
        saved_size = orig_size - new_size
        ratio = (new_size / orig_size * 100) if orig_size > 0 else 0
        
        # 结果弹窗包含所有信息
        msg = (f"任务全部完成！\n"
               f"──────────────────\n"
               f"✅ 成功处理: {success} 张\n"
               f"❌ 处理失败: {fail} 张\n"
               f"──────────────────\n"
               f"🚫 扫描阶段已跳过:\n"
               f"• 因日期较新: {self.skipped_by_date} 张\n"
               f"• 因小于阈值: {self.skipped_by_size} 张\n"
               f"──────────────────\n"
               f"📦 压缩前占用: {format_size(orig_size)}\n"
               f"📦 压缩后占用: {format_size(new_size)}\n"
               f"🎉 节省空间: {format_size(saved_size)}\n"
               f"📉 本次压缩比: {ratio:.1f}%")
               
        MessageBox("处理报告", msg, self).exec()
        self.lbl_status.setText("")

    def toggle_pause(self):
        if self.current_state == "COMPRESSING":
            self.compress_thread.pause()
            self.update_ui_state("PAUSED")
        elif self.current_state == "PAUSED":
            self.compress_thread.resume()
            self.update_ui_state("COMPRESSING")

    def stop_process(self):
        if self.current_state == "SCANNING":
            if hasattr(self, 'scan_thread') and self.scan_thread.isRunning():
                self.btn_stop.setEnabled(False)
                self.scan_thread.stop()
                self.lbl_status.setText("扫描已取消。")
                self.update_ui_state("INIT")
        elif self.current_state in["COMPRESSING", "PAUSED"]:
            if hasattr(self, 'compress_thread') and self.compress_thread.isRunning():
                self.btn_stop.setEnabled(False)
                self.btn_pause.setEnabled(False)
                self.lbl_status.setText("正在向所有线程发送停止信号，请稍候...")
                self.compress_thread.stop()

    def start_debug_scan(self):
        folder = self.folder_input.text()
        if not folder or not os.path.exists(folder):
            MessageBox("错误", "请先在顶部选择一个有效的文件夹路径！", self).exec()
            return
            
        self.btn_scan_exts.setEnabled(False)
        self.text_debug_output.setText("🚀 正在极速扫描中...\n(扫描大文件夹可能需要一点时间)")
        self.debug_scan_thread = ScanExtensionsThread(folder)
        self.debug_scan_thread.scan_progress.connect(self.update_debug_scan_progress)
        self.debug_scan_thread.finished_scan.connect(self.on_debug_scan_finished)
        self.debug_scan_thread.start()

    def update_debug_scan_progress(self, scanned):
        self.text_debug_output.setText(f"🚀 正在极速扫描中...\n\n当前已扫描文件数: {scanned} 个\n请耐心等待...")

    def on_debug_scan_finished(self, ext_counts):
        self.btn_scan_exts.setEnabled(True)
        if not ext_counts:
            self.text_debug_output.setText("该文件夹（含子目录）内没有任何文件。")
            return
        sorted_exts = sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)
        lines =[f"✅ 扫描完成！共发现 {len(sorted_exts)} 种文件格式：\n"]
        for ext, count in sorted_exts:
            lines.append(f"• {ext} : {count} 个")
        self.text_debug_output.setText("\n".join(lines))

if __name__ == '__main__':
    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    window = CompressApp()
    window.show()
    sys.exit(app.exec())