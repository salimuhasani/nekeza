# Masai / Ramkey — Vercel + Supabase

App ya Flask (sales, products, employees, defects) tayari kwa **GitHub → Vercel** na database **Supabase (Postgres)**.

Logic na templates zimebaki vile vile. Imeongezwa tu support ya Postgres + env vars.

## 1. Supabase

1. Fungua https://supabase.com → New Project
2. **Project Settings → Database → Connection string → URI**
3. Copy URL (mfano `postgresql://postgres:...@db.xxx.supabase.co:5432/postgres`)

## 2. Environment Variables (Vercel)

| Variable | Mfano | Lazima |
|----------|--------|--------|
| `DATABASE_URL` | `postgresql://postgres:PASSWORD@db.xxx.supabase.co:5432/postgres` | Ndiyo |
| `SECRET_KEY` | random string ≥ 32 chars | Ndiyo |
| `ADMIN_USERNAME` | `admin` | Hapana (default admin) |
| `ADMIN_PASSWORD` | password yako | Hapana (default admin123) |
| `COOKIE_SECURE` | `true` | Ndiyo kwenye production |

## 3. GitHub

```bash
cd masai_vercel
git init
git add .
git commit -m "Masai app ready for Vercel + Supabase"
git branch -M main
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```

## 4. Vercel

1. https://vercel.com → Add New Project → Import GitHub repo
2. Framework: Other
3. Root directory: (repo root)
4. Ongeza Environment Variables hapo juu
5. Deploy

Baada ya deploy, fungua site → login `admin` / password uliyoweka.

## 5. Local (SQLite)

Bila `DATABASE_URL`, app inatumia `database.db` (SQLite) automatically:

```bash
pip install -r requirements.txt
python app.py
```

## Muhimu

- Usichange templates / routes — zimebaki kama zilivyokuwa
- Tables zinaundwa automatic (init_db) kwenye request ya kwanza
- Default admin inaundwa kama haipo
- Badilisha `ADMIN_PASSWORD` mara moja baada ya deploy
