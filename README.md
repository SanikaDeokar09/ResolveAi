# ResolveAI (Windows / PowerShell quick start)

Requires **Python 3.10+** (3.11 or 3.12 recommended).

```powershell
cd C:\path\to\resolveai

# 1. Virtual environment (do NOT use `source .venv/bin/activate` on Windows)
py -3.12 -m venv .venv          # or: py -3.11 / python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If PowerShell blocks the script ("running scripts is disabled"):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
# then run the Activate.ps1 line again.

# 2. Dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3. Verify the APIs this project relies on exist in your installed versions
pip list | Select-String "langgraph|langchain|pydantic|streamlit|pytest"
python -c "from langgraph.types import interrupt, Command, PregelTask; print('PregelTask fields:', PregelTask._fields)"
python -c "from langgraph.checkpoint.sqlite import SqliteSaver; print('SqliteSaver import OK')"
#   -> 'interrupts' must appear in the PregelTask fields.

# 4. Tests
pytest -q

# 5. Dashboard
streamlit run app.py            # opens http://localhost:8501
```

Optional:

```powershell
$env:RESOLVEAI_DB = "C:\temp\resolveai-test.db"   # use a different database file
streamlit run app.py
pip freeze > requirements.lock.txt                # pin the versions that passed your tests
```

The default database is `data\resolveai.db` inside the project folder. Delete it to reset all cases.
