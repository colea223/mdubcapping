@echo off
REM Runs excel/import_bet_log.py using the project's own virtual environment,
REM logging output so you can check whether a scheduled run actually worked
REM without needing to watch it happen. Meant to be pointed at by a Windows
REM Task Scheduler task (see the setup steps Claude gave you in chat) rather
REM than double-clicked directly, though double-clicking it works fine too --
REM it'll just flash a window open and closed.
"C:\Users\deene\Mountain Dub Handicapping\.venv\Scripts\python.exe" "C:\Users\deene\Mountain Dub Handicapping\excel\import_bet_log.py" >> "C:\Users\deene\Mountain Dub Handicapping\excel\import_bet_log_log.txt" 2>&1
