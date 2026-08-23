# ALPHA ANALYZER — NOTIFICATION PANEL FIX

Fixed the startup NameError shown by Streamlit.

Cause:
`render_notification_panel()` was called before the function definition.

Fix:
The call now occurs after the notification-panel helper is defined and before
the main page routing/auto-refresh section.

Trading logic and notification behavior are unchanged.
