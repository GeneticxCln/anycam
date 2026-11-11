"""The "Add phone camera" dialog: a QR code the phone scans to connect."""

from __future__ import annotations

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .phone import firewall
from .phone.auth import TOKEN_TTL_S
from .phone.tls import qr_png


class PhoneEvents(QObject):
    """Carries phone-link events from network threads to the UI thread."""

    event = Signal(str, str, str)  # event, device id, name/detail


class PhonePairDialog(QDialog):
    """Show a one-time QR code; close once a phone streams.

    ``device`` holds the phone that connected (paired now or earlier).
    """

    def __init__(self, service, parent=None, log=None) -> None:
        super().__init__(parent)
        self.service = service
        self.log = log or (lambda message: None)
        self.device = ""
        self.device_name = ""
        self.setWindowTitle("Add phone camera")
        self.setModal(True)
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setMinimumSize(300, 300)
        self.qr.setStyleSheet("background: white; border-radius: 8px;")
        row.addWidget(self.qr)

        steps = QLabel(
            "<ol style='margin-left:-20px'>"
            "<li>Put the phone on the same Wi-Fi as this computer.</li>"
            "<li>Scan the code with the phone's camera and open the link.</li>"
            "<li>The browser warns about the certificate: tap "
            "<b>Advanced → Proceed</b>. The certificate is this computer's own; "
            "compare the fingerprint below if you want to be sure.</li>"
            "<li>Allow the camera. The phone appears in your scene.</li>"
            "</ol>"
            "<p>Next time, open the same page on the phone again (or scan a new "
            "code); it reconnects without pairing.</p>")
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.TextFormat.RichText)
        steps.setMinimumWidth(320)
        row.addWidget(steps, 1)
        layout.addLayout(row)

        self.url = QLabel()
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.setWordWrap(True)
        layout.addWidget(self.url)
        self.fingerprint = QLabel(
            f"Certificate fingerprint (SHA-256): {service.fingerprint}")
        self.fingerprint.setWordWrap(True)
        self.fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fingerprint.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self.fingerprint)
        # The firewall: a blocked port means the phone's page never loads.
        # Offer a one-click fix (system password prompt) instead of a command.
        self.port = int(getattr(service, "port", 8443))
        self.firewall_bar = QFrame()
        self.firewall_bar.setStyleSheet(
            "QFrame { background: #5c4813; border-radius: 6px; } "
            "QLabel { color: #ffe9a8; }")
        bar = QHBoxLayout(self.firewall_bar)
        self.firewall_text = QLabel(
            "This computer's firewall blocks phones. Allow them once:")
        self.firewall_text.setWordWrap(True)
        self.firewall_text.setTextFormat(Qt.TextFormat.PlainText)
        bar.addWidget(self.firewall_text, 1)
        self.firewall_button = QPushButton("Allow phones")
        self.firewall_button.clicked.connect(self.allow_through_firewall)
        bar.addWidget(self.firewall_button)
        layout.addWidget(self.firewall_bar)
        self._firewall_process: QProcess | None = None
        self._firewall_job: dict | None = None
        self._firewall_timer = QTimer(self)
        self._firewall_timer.setInterval(150)
        self._firewall_timer.timeout.connect(self._poll_firewalld)
        self.host = str(getattr(service, "host", "") or "")
        self.firewall_backend = ""
        self.firewall_bar.setVisible(False)
        self._detect_firewall()
        self.status = QLabel("Waiting for the phone…")
        # The phone chooses its own name (up to 40 characters, markup
        # included): show it as text, never as rich text.
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.new_code = QPushButton("New code")
        self.new_code.clicked.connect(self.refresh_code)
        buttons.addButton(self.new_code, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # A code works once and only for a few minutes; renew it before it
        # expires so a dialog left open never shows a dead code.
        self._renew = QTimer(self)
        self._renew.setInterval(int((TOKEN_TTL_S - 30) * 1000))
        self._renew.timeout.connect(self.refresh_code)
        self._renew.start()
        self.refresh_code()

    def _detect_firewall(self) -> None:
        """Find out whether the firewall blocks phones, without stalling the UI.

        The checks are D-Bus calls (2-5 s timeouts when a service hangs).  They
        run on a thread; the dialog waits a moment for the usual instant answer
        and otherwise shows itself and fills the warning in when it arrives.
        """
        result: dict = {}

        def work() -> None:
            try:
                result["backend"] = firewall.backend()
                result["blocked"] = bool(firewall.blocks(self.port, self.host))
            except Exception:  # noqa: BLE001 - unknown is "not blocked"
                result.setdefault("backend", "")
                result["blocked"] = False
            result["done"] = True

        import threading

        thread = threading.Thread(target=work, name="anycam-firewall-check", daemon=True)
        thread.start()
        thread.join(0.3)
        self._detect_result = result
        self._detect_timer = QTimer(self)
        self._detect_timer.setInterval(150)
        self._detect_timer.timeout.connect(self._apply_firewall_detection)
        self._apply_firewall_detection()
        if not result.get("done"):
            self._detect_timer.start()

    def _apply_firewall_detection(self) -> None:
        result = self._detect_result
        if not result.get("done"):
            return
        self._detect_timer.stop()
        self.firewall_backend = str(result.get("backend") or "")
        self.firewall_bar.setVisible(bool(result.get("blocked")))

    def allow_through_firewall(self, *, retried: bool = False) -> None:
        """Open the phone port; the desktop asks for the password."""
        if self.firewall_backend == "firewalld":
            self._allow_with_firewalld(retried)
            return
        command = firewall.allow_command(self.port)
        if command is None or self._firewall_process is not None:
            return
        self.firewall_button.setEnabled(False)
        self.firewall_text.setText("Enter your password in the system prompt…")
        process = QProcess(self)
        process.finished.connect(
            lambda code, _status: self._firewall_done(code, retried))
        process.errorOccurred.connect(
            lambda error: self.log(f"firewall: pkexec did not run ({error})"))
        self._firewall_process = process
        self.log("firewall: " + " ".join(command))
        process.start(command[0], command[1:])

    def _firewall_done(self, code: int, retried: bool = False) -> None:
        process, self._firewall_process = self._firewall_process, None
        output = ""
        if process is not None:
            output = bytes(process.readAllStandardError()).decode(errors="replace").strip()
            process.deleteLater()
        self.log(f"firewall: pkexec exit {code}: {output or 'no output'}")
        if code == 0 and (not firewall.ufw_blocks(self.port)
                          or not firewall.ufw_rules_readable()):
            # ufw allow succeeded.  Where its rules cannot be read (Debian,
            # Ubuntu) the check cannot confirm it, so remember it instead.
            firewall.remember_allowed(self.port)
            self.firewall_bar.setVisible(False)
            self.status.setText("Phones are allowed through the firewall. "
                                "Scan the code now.")
            return
        agent = firewall.installed_agent()
        if firewall.no_agent(output) and agent and not retried:
            # The session never started its password-prompt service; start
            # the installed one and ask again.
            self.firewall_text.setText("Starting the system password prompt…")
            started = QProcess.startDetached(agent, [])
            self.log(f"firewall: no polkit agent running; started {agent}: {started}")
            # The agent needs a moment to register with polkit.
            QTimer.singleShot(2500, lambda: self.allow_through_firewall(retried=True))
            return
        self.firewall_button.setEnabled(True)
        if firewall.no_agent(output):
            message = ("This desktop has no password prompt service running "
                       "(polkit agent), so the firewall cannot be changed from here.")
        elif code == 126:
            # pkexec: the prompt was dismissed.
            message = "Cancelled. Click to try again:"
        elif code == 127:
            message = f"Not authorized: {output or 'the password was not accepted'}. Try again:"
        else:
            message = f"Could not change the firewall: {output or f'exit {code}'}. Try again:"
        self.firewall_text.setText(message)

    def _allow_with_firewalld(self, retried: bool) -> None:
        """firewalld over D-Bus on a thread; polkit shows the password prompt."""
        if self._firewall_job is not None:
            return
        self.firewall_button.setEnabled(False)
        self.firewall_text.setText("Enter your password in the system prompt…")
        job: dict = {"retried": retried}
        self._firewall_job = job

        def work() -> None:
            job["error"] = firewall.firewalld_allow(self.port, self.host)
            job["done"] = True

        import threading

        threading.Thread(target=work, name="anycam-firewalld", daemon=True).start()
        self.log(f"firewall: firewalld addPort {self.port}/tcp")
        self._firewall_timer.start()

    def _poll_firewalld(self) -> None:
        job = self._firewall_job
        if job is None or not job.get("done"):
            return
        self._firewall_timer.stop()
        self._firewall_job = None
        error = job.get("error", "")
        self.log(f"firewall: firewalld result: {error or 'ok'}")
        if not error and not firewall.firewalld_blocks(self.port, self.host):
            self.firewall_bar.setVisible(False)
            self.status.setText("Phones are allowed through the firewall. Scan the code now.")
            return
        agent = firewall.installed_agent()
        if firewall.auth_missing(error) and agent and not job.get("retried"):
            # Same as with ufw: no polkit agent in this session; start it.
            self.firewall_text.setText("Starting the system password prompt…")
            QProcess.startDetached(agent, [])
            QTimer.singleShot(2500, lambda: self.allow_through_firewall(retried=True))
            return
        self.firewall_button.setEnabled(True)
        self.firewall_text.setText(f"Could not change the firewall: {error}. Try again:")

    def refresh_code(self) -> None:
        link = self.service.pairing_url()
        png = qr_png(link)
        pixmap = QPixmap()
        if png and pixmap.loadFromData(png, "PNG"):
            self.qr.setPixmap(pixmap.scaled(
                300, 300, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation))
        else:
            self.qr.setText("QR code unavailable:\ninstall qrencode or segno\n"
                            "and open the link below on the phone.")
        self.url.setText(f"Or open on the phone: <b>{self.service.page_url()}</b>")
        self.status.setText("Waiting for the phone…")

    def on_phone_event(self, event: str, device: str, detail: str) -> None:
        if event == "paired":
            self.status.setText(f"Paired with {detail}. Waiting for its camera…")
            # The code was used; show a fresh one for the next phone.
            self.refresh_code()
            self.status.setText(f"Paired with {detail}. Waiting for its camera…")
        elif event == "connecting":
            self.status.setText(f"{detail} is connecting…")
        elif event == "failed":
            self.status.setText(f"The phone could not connect: {detail}. Check that "
                                "both are on the same Wi-Fi and the firewall allows it.")
        elif event == "streaming":
            self.device = device
            self.device_name = detail
            self.accept()
