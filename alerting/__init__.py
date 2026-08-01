"""Alerting package.

Public seam re-exported here: ``Dispatcher``, ``DispatchResult``,
``build_alert``, ``AlertError``. ``GmailSender`` / ``TwilioSender`` are NOT
re-exported -- re-exporting ``TwilioSender`` would force a ``twilio`` import on
``import alerting``. Import senders from their modules
(``alerting.email_alert`` / ``alerting.sms_alert``) when wiring production.
"""

from __future__ import annotations

from errors import AlertError

from alerting.dispatch import Dispatcher, DispatchResult
from alerting.formatting import build_alert

__all__ = ["Dispatcher", "DispatchResult", "build_alert", "AlertError"]
