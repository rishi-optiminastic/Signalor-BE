import logging
import os

from django.apps import AppConfig

logger = logging.getLogger("apps")


class AnalyzerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analyzer"
    verbose_name = "GEO Analyzer"

    def ready(self):
        # Only start the scheduler in the main process.
        # - Dev (runserver): Django sets RUN_MAIN=true in the reloaded child.
        # - Production (gunicorn --preload): start in all workers is fine.
        # Skip during migrations, tests, shell, and other management commands.
        if os.environ.get("DISABLE_WEEKLY_SCHEDULER") == "true":
            return

        run_main = os.environ.get("RUN_MAIN")
        is_gunicorn = "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")
        if run_main != "true" and not is_gunicorn:
            return

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from django.core.management import call_command

            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(
                lambda: call_command("send_weekly_emails"),
                trigger=CronTrigger(day_of_week="fri", hour=9, minute=0),
                id="weekly_email_report",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            scheduler.start()
            logger.info("Weekly email scheduler started — fires every Friday at 09:00 UTC")
        except Exception:
            logger.exception("Failed to start weekly email scheduler")
