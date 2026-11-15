git config user.email eval@example.com
git config user.name eval
printf 'def total(prices):\n    return round(sum(prices))\n' > calc.py
git add calc.py && git commit -qm "Add calc"
printf 'def total(prices):\n    return round(sum(prices), 2)\n' > calc.py
echo 'half-written idea, not for the repo' > notes-draft.txt
