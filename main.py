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

def compress_single_image(filepath, quality, keep_format, pause_event, stop_event):
    """独立的压缩函数，支持暂停和停止监听"""
    try:
        # 1. 检查是否被要求停止
        if stop_event.is_set():
            return False, 0, 0, "已停止", filepath
            
        # 2. 检查是否被要求暂停 (wait 会阻塞直到事件被 set)
        pause_event.wait()
        
        # 唤醒后再次检查是否停止
        if stop_event.is_set():
            return False, 0, 0, "已停止", filepath

        original_size = os.path.getsize(filepath)
        img = Image.open(filepath)
        
        target_path = filepath
        is_png = filepath.lower().endswith('.png')

        # 如果不保留原格式，且原图是 PNG，则准备转换为 JPG 以节省空间
        if not keep_format and is_png:
            target_path = os.path.splitext(filepath)[0] + '.jpg'
        
        # 解决调色板透明度警告 (Palette images with Transparency)
        if img.mode == 'P':
            img = img.convert('RGBA')
            
        # 如果目标是 JPG 且带有透明度，需要垫一个纯白背景
        if img.mode in ('RGBA', 'LA') and target_path.lower().endswith(('.jpg', '.jpeg')):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        # 如果不是 RGB 也不是要存为带透明度的 PNG，强转为 RGB
        elif img.mode != 'RGB' and target_path.lower().endswith(('.jpg', '.jpeg')):
            img = img.convert('RGB')
            
        # 保存图片
        img.save(target_path, optimize=True, quality=quality)
        
        # 如果进行了格式转换（PNG -> JPG）且成功保存，则删除原 PNG 文件
        if target_path != filepath and os.path.exists(target_path):
            os.remove(filepath)
            
        new_size = os.path.getsize(target_path)
        
        return True, original_size, new_size, "", target_path
    except Exception as e:
        return False, 0, 0, str(e), filepath


class ImageScanThread(QThread):
    """后台扫描线程：负责找出所有待处理的图片"""
    scan_progress = Signal(int, int) # (已扫描文件数, 符合条件数)
    finished_scan = Signal(list, int) # (待处理文件路径列表, 因日期较新跳过数)

    def __init__(self, folder, exts, cutoff_timestamp):
        super().__init__()
        self.folder = folder
        self.exts = tuple(exts)
        self.cutoff = cutoff_timestamp
        self.is_stopped = False

    def stop(self):
        self.is_stopped = True

    def run(self):
        target_files = []
        skipped_by_date = 0
        scanned_count = 0
        dirs_to_process = [self.folder]

        while dirs_to_process:
            if self.is_stopped:
                break
            current_dir = dirs_to_process.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if self.is_stopped:
                            break
                        if entry.is_dir(follow_symlinks=False):
                            dirs_to_process.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            scanned_count += 1
                            if entry.name.lower().endswith(self.exts):
                                if entry.stat().st_mtime < self.cutoff:
                                    target_files.append(entry.path)
                                else:
                                    skipped_by_date += 1
                            
                            # 每 1000 个文件更新一次进度，防止 UI 卡顿
                            if scanned_count % 1000 == 0:
                                self.scan_progress.emit(scanned_count, len(target_files))
            except PermissionError:
                continue

        self.finished_scan.emit(target_files, skipped_by_date)


class CompressThread(QThread):
    """后台压缩线程：负责并发压缩图片"""
    progress_update = Signal(int, int) # 当前进度, 总数
    finished_work = Signal(int, int, object, object) # 成功数, 失败数, 原始大小, 压缩后大小

    def __init__(self, target_files, quality, keep_format):
        super().__init__()
        self.target_files = target_files
        self.quality = quality
        self.keep_format = keep_format
        
        # 线程控制事件
        self.pause_event = threading.Event()
        self.pause_event.set() # 初始状态为 True (非暂停)
        self.stop_event = threading.Event()

    def pause(self):
        self.pause_event.clear()

    def resume(self):
        self.pause_event.set()

    def stop(self):
        self.stop_event.set()
        self.pause_event.set() # 防止线程在暂停状态下死锁，需要唤醒它们执行退出判断

    def run(self):
        total = len(self.target_files)
        success_count = fail_count = total_original_size = total_new_size = 0
        
        if total == 0 or self.stop_event.is_set():
            self.finished_work.emit(0, 0, 0, 0)
            return

        max_workers = max(1, multiprocessing.cpu_count() - 1)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {
                executor.submit(compress_single_image, path, self.quality, self.keep_format, self.pause_event, self.stop_event): path 
                for path in self.target_files
            }
            
            completed = 0
            for future in as_completed(future_to_path):
                # 即使触发了 stop_event，也要把迭代器走完，以清理内存并安全退出
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
                
                # 【修改点】：避免频繁抛出信号，改为每 100 张更新一次，或者是最后一张时更新
                if (completed % 100 == 0 or completed == total) and not self.stop_event.is_set():
                    self.progress_update.emit(completed, total)

        self.finished_work.emit(success_count, fail_count, total_original_size, total_new_size)


# 省略 ScanExtensionsThread，保持你原有的逻辑即可
class ScanExtensionsThread(QThread):
    finished_scan = Signal(dict)
    scan_progress = Signal(int)

    def __init__(self, folder):
        super().__init__()
        self.folder = folder

    def run(self):
        ext_counts = {}
        scanned_count = 0
        dirs_to_process = [self.folder]

        while dirs_to_process:
            current_dir = dirs_to_process.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            dirs_to_process.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            scanned_count += 1
                            ext = os.path.splitext(entry.name)[1].lower()
                            if not ext:
                                ext = "[无后缀名文件]"
                            ext_counts[ext] = ext_counts.get(ext, 0) + 1
                            if scanned_count % 1000 == 0:
                                self.scan_progress.emit(scanned_count)
            except PermissionError:
                continue
        self.finished_scan.emit(ext_counts)


class CompressApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("图片缓存深度压缩工具")
        self.resize(650, 550)
        setTheme(Theme.AUTO)

        # 内部核心状态
        self.current_state = "INIT"  # INIT, SCANNING, READY, COMPRESSING, PAUSED
        self.target_files = []
        self.skipped_by_date = 0

        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # ====== 1. 顶部：全局文件夹选择 ======
        folder_layout = QHBoxLayout()
        self.folder_input = LineEdit()
        self.folder_input.setPlaceholderText("请选择文件夹路径...")
        self.folder_input.setReadOnly(True)
        self.btn_browse = PushButton("选择文件夹")
        self.btn_browse.clicked.connect(self.browse_folder)
        folder_layout.addWidget(self.folder_input)
        folder_layout.addWidget(self.btn_browse)
        main_layout.addLayout(folder_layout)

        # ====== 2. 核心：标签页系统 ======
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # --- 标签页 A：压缩工具 ---
        self.tab_compress = QWidget()
        self.setup_compress_tab()
        self.tabs.addTab(self.tab_compress, "压缩工具")

        # --- 标签页 B：调试工具 ---
        self.tab_debug = QWidget()
        self.setup_debug_tab()
        self.tabs.addTab(self.tab_debug, "调试工具")

    def setup_compress_tab(self):
        layout = QVBoxLayout(self.tab_compress)
        layout.setSpacing(20)
        layout.setContentsMargins(20, 20, 20, 20)

        # 格式选项
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

        # 压缩方式
        quality_layout = QHBoxLayout()
        quality_layout.addWidget(StrongBodyLabel("压缩质量:"))
        self.combo_quality = ComboBox()
        self.combo_quality.addItems([
            "低质量 (40%) - 极限空间", 
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
        quality_layout.addStretch(1)
        layout.addLayout(quality_layout)

        # 时间筛选
        date_layout = QHBoxLayout()
        date_layout.addWidget(StrongBodyLabel("时间筛选:"))
        date_layout.addWidget(BodyLabel("仅处理早于:"))
        
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

        # 绑定参数变更事件（改变扫描条件将使现存缓存失效）
        self.cb_jpg.stateChanged.connect(self.on_scan_settings_changed)
        self.cb_png.stateChanged.connect(self.on_scan_settings_changed)
        self.combo_date.currentIndexChanged.connect(self.on_scan_settings_changed)
        self.date_picker.dateChanged.connect(self.on_scan_settings_changed)

        # 进度条与状态
        self.progress_bar = ProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.lbl_status = BodyLabel("")
        layout.addWidget(self.lbl_status)

        # ============ 控制按钮组 ============
        btn_layout = QHBoxLayout()
        self.btn_action = PushButton("1. 扫描图片") # 核心按钮 (扫描/开始压缩 切换)
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
        # ... (此处省略 setup_debug_tab 代码，与你上一版完全一致) ...
        layout = QVBoxLayout(self.tab_debug)
        self.btn_scan_exts = PushButton("扫描文件夹内所有存在的文件格式")
        self.btn_scan_exts.clicked.connect(self.start_debug_scan)
        layout.addWidget(self.btn_scan_exts)
        self.text_debug_output = TextEdit()
        self.text_debug_output.setReadOnly(True)
        layout.addWidget(self.text_debug_output)

    # ============ UI 状态机逻辑 ============
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
            self.progress_bar.setRange(0, 0) # 不确定进度条动画
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
        """当改变格式或日期时，如果处于 READY 状态，让用户必须重新扫描"""
        if self.current_state == "READY":
            self.target_files = []
            self.update_ui_state("INIT")
            self.lbl_status.setText("扫描条件已更改，请重新扫描文件目录。")

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

    # ============ 核心流程控制 ============
    def on_action_clicked(self):
        """核心按钮入口：根据状态决定是扫描还是压缩"""
        if self.current_state == "INIT":
            self.start_scan()
        elif self.current_state == "READY":
            self.start_compression()

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
            
        # 解析时间筛选
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

        self.update_ui_state("SCANNING")
        self.lbl_status.setText("🚀 正在极速扫描目录中，请稍候...")

        self.scan_thread = ImageScanThread(folder, exts, cutoff_timestamp)
        self.scan_thread.scan_progress.connect(self.update_scan_progress)
        self.scan_thread.finished_scan.connect(self.on_scan_finished)
        self.scan_thread.start()

    def update_scan_progress(self, scanned, found):
        self.lbl_status.setText(f"🔍 扫描中... 已检查文件: {scanned} 个 | 发现待处理: {found} 张")

    def on_scan_finished(self, target_files, skipped_by_date):
        if self.current_state != "SCANNING":
            return # 有可能已被强行中止

        self.target_files = target_files
        self.skipped_by_date = skipped_by_date
        
        if len(target_files) == 0:
            self.update_ui_state("INIT")
            self.lbl_status.setText(f"扫描完毕。未发现符合条件的图片。(因日期较新跳过: {skipped_by_date} 张)")
        else:
            self.update_ui_state("READY")
            self.lbl_status.setText(f"✅ 扫描完成！共发现 {len(target_files)} 张符合条件的图片等待压缩。(已跳过 {skipped_by_date} 张)")

    def start_compression(self):
        w = MessageBox(
            "高危操作确认",
            f"⚠️ 警告：\n图片压缩将直接覆盖原文件且无法恢复！\n即将并发处理 {len(self.target_files)} 张图片，您确定要继续吗？",
            self
        )
        if not w.exec():
            return 

        quality_index = self.combo_quality.currentIndex()
        if quality_index == 3: quality = self.spin_custom_quality.value()
        else: quality = {0: 40, 1: 60, 2: 80}[quality_index]
            
        keep_format = self.cb_keep_format.isChecked()

        self.update_ui_state("COMPRESSING")
        self.progress_bar.setValue(0)
        self.lbl_status.setText("正在分配线程...")

        self.compress_thread = CompressThread(self.target_files, quality, keep_format)
        self.compress_thread.progress_update.connect(self.update_compress_progress)
        self.compress_thread.finished_work.connect(self.compression_finished)
        self.compress_thread.start()

    def update_compress_progress(self, current, total):
        self.lbl_status.setText(f"⚙️ 并发压缩中: {current} / {total} (占用 CPU 多核)")
        percent = int((current / total) * 100)
        self.progress_bar.setValue(percent)

    def compression_finished(self, success, fail, orig_size, new_size):
        self.target_files = [] # 处理完成清空缓存
        self.update_ui_state("INIT")
        
        saved_size = orig_size - new_size
        ratio = (new_size / orig_size * 100) if orig_size > 0 else 0
        
        msg = (f"处理结束！\n"
               f"──────────────────\n"
               f"✅ 成功压缩: {success} 张\n"
               f"❌ 失败/报错: {fail} 张\n"
               f"⏳ 因日期较新跳过: {self.skipped_by_date} 张\n"
               f"──────────────────\n"
               f"📦 压缩前占用: {format_size(orig_size)}\n"
               f"📦 压缩后占用: {format_size(new_size)}\n"
               f"🎉 节省空间: {format_size(saved_size)}\n"
               f"📉 整体压缩比: {ratio:.1f}%")
               
        MessageBox("任务结束", msg, self).exec()
        self.lbl_status.setText("")

    # ============ 暂停与停止逻辑 ============
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
                
        elif self.current_state in ["COMPRESSING", "PAUSED"]:
            if hasattr(self, 'compress_thread') and self.compress_thread.isRunning():
                self.btn_stop.setEnabled(False)
                self.btn_pause.setEnabled(False)
                self.lbl_status.setText("正在向所有线程发送停止信号，请稍候...")
                self.compress_thread.stop()
                # 线程响应后会自动调用 compression_finished，在那边将状态恢复为 INIT

    # ====== 调试工具逻辑（原有） ======
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
        lines = [f"✅ 扫描完成！共发现 {len(sorted_exts)} 种文件格式：\n"]
        for ext, count in sorted_exts:
            lines.append(f"• {ext} : {count} 个")
        self.text_debug_output.setText("\n".join(lines))


if __name__ == '__main__':
    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    window = CompressApp()
    window.show()
    sys.exit(app.exec())