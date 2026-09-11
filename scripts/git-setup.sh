#!/bin/bash
# One-time: turn the live tree into a git repository, safely.
#
# The point is not convenience. This tree holds .env, a Telegram session and
# every financial database; a careless `git add -A` here would publish all of
# it. So the ignore rules and a refusing pre-commit hook go in BEFORE the first
# file is ever staged, and the first commit is scanned before it is made.
#
# Run once, from the live tree:   bash scripts/git-setup.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
echo "дерево: $ROOT"

if [ -d .git ]; then
  echo "Здесь уже есть репозиторий. Ничего не делаю."
  exit 0
fi

# ---------------------------------------------------------------- ignore rules
cat > .gitignore <<'IGNORE'
# Byte code
__pycache__/
*.pyc
*.pyo

# Secrets and credentials. Never commit any of these.
.env
.env.*
*.key
*.pem
*.p12
desktop/*.session
desktop/*.session-journal

# Runtime and financial state. Every path below is an artifact the backup
# owns (see runtime-state-manifest.json) - a different lifecycle from source.
# It includes the PAPER/SHADOW state directories and the API budget.
data/
*.sqlite
*.sqlite3
*.sqlite-wal
*.sqlite-shm
*.sqlite3-wal
*.sqlite3-shm
*.db

# Local scratch
*.tar.gz
*.tgz
*.zip
*.orig
*.rej
backup-*/
IGNORE

# ------------------------------------------------------------------ safety net
# .gitignore is advisory: `git add -f`, or a path added before the rule existed,
# walks straight past it. This hook is the part that actually refuses.
mkdir -p .git-hooks
cat > .git-hooks/pre-commit <<'HOOK'
#!/bin/bash
# Refuse to commit secrets or financial state, however they got staged.
bad=$(git diff --cached --name-only --diff-filter=ACM | grep -Ei \
  '(^|/)\.env($|\.)|\.key$|\.pem$|\.p12$|\.session$|\.sqlite3?($|-wal|-shm)$|^data/' || true)
if [ -n "$bad" ]; then
  echo "ОТКАЗ: в коммит попали файлы состояния или секреты:" >&2
  echo "$bad" | sed 's/^/  /' >&2
  echo >&2
  echo "Уберите их:  git restore --staged <файл>" >&2
  exit 1
fi
HOOK
chmod +x .git-hooks/pre-commit

# ------------------------------------------------------------------ initialise
git init -q .
git config core.hooksPath .git-hooks
git config user.name  "$(git config --global user.name  || echo 'WalletHunter server')"
git config user.email "$(git config --global user.email || echo 'server@wallethunter.local')"
git symbolic-ref HEAD refs/heads/main
git add -A

# ------------------------------------------------------- verify before committing
echo
echo "К коммиту подготовлено файлов: $(git diff --cached --name-only | wc -l)"
leak=$(git diff --cached --name-only | grep -Ei \
  '(^|/)\.env($|\.)|\.key$|\.pem$|\.p12$|\.session$|\.sqlite3?($|-wal|-shm)$|^data/' || true)
if [ -n "$leak" ]; then
  echo
  echo "ОСТАНОВЛЕН. В индекс попало то, что публиковать нельзя:" >&2
  echo "$leak" | sed 's/^/  /' >&2
  echo >&2
  echo "Коммит НЕ сделан. Пришлите этот список — разберёмся, откуда взялось." >&2
  exit 1
fi

git commit -q -m "Baseline: live server tree as deployed

First commit of the running tree, taken from the server rather than from an
older upload, so history starts where the code actually is. Runtime state,
credentials and the financial databases are excluded by .gitignore and by a
pre-commit hook that refuses them even when staged explicitly."

echo
echo "Готово. Коммит:"
git --no-pager log --oneline -1
echo
echo "Состояние и секреты не отслеживаются:"
git status --porcelain --ignored | grep '^!!' | head -8 | sed 's/^!! /  игнорируется: /'
echo
echo "Дальше — ключ для GitHub:"
echo "  ssh-keygen -t ed25519 -f ~/.ssh/github-deploy -N '' -C wallethunter-server"
echo "  cat ~/.ssh/github-deploy.pub"
