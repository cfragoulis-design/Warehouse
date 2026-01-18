# Inventory v1 (Γενική + Κεντρικό + Υποκατάστημα)

## Τι κάνει
- Γενική αποθήκη (total)
- Κατανομή σε 2 καταστήματα
- Ενδοδιακίνηση ως Transfer (draft -> confirm)
- Ρόλοι (admin / staff)
- Login με PIN 6 ψηφίων
- Export σε Excel: transfers & stock snapshot

## Περιβάλλον (Railway)
Απαραίτητα env vars:
- SECRET_KEY: τυχαίο μεγάλο string
- DATABASE_URL: Railway Postgres (αυτόματο)
- INITIAL_ADMIN_PIN: 6 ψηφία (πρώτο run μόνο)
- INITIAL_ADMIN2_PIN: 6 ψηφία (πρώτο run μόνο)
- APP_NAME: (optional)

### Start command
Railway μπορεί να τρέξει το Procfile αυτόματα ή βάλε:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Local run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="dev-secret"
export INITIAL_ADMIN_PIN="123456"
export INITIAL_ADMIN2_PIN="234567"
uvicorn app.main:app --reload
```

## Σημείωση για πρώτη εκκίνηση
Αν η βάση είναι άδεια, η εφαρμογή απαιτεί τα INITIAL_ADMIN_PIN/INITIAL_ADMIN2_PIN, αλλιώς θα σταματήσει.
