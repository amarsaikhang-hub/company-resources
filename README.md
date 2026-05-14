# Company Resources

Компанийн нөөцийн сан — ажилтан, тоног төхөөрөмж, гэрээ + AI тулгалт.

## Суулгах

```bash
cd ~/Downloads/company-resources
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env-д ANTHROPIC_API_KEY нэмэх
```

## Ажиллуулах

```bash
source venv/bin/activate
export $(cat .env | xargs)
python app.py
```

Browser-т: `http://localhost:5001`

Нэвтрэх: tender-dashboard-тай ижил (admin / admin123).

## Хуудсууд

- `/` — Нүүр (статистик)
- `/employees` — Ажилтан (регистр, диплом, НДШ)
- `/equipment` — Тоног төхөөрөмж (машин, техник)
- `/contracts` — Ижил төстэй гэрээ
- `/match` — AI тулгалт: шаардлага ↔ нөөц

## Бүтэц

- **PostgreSQL**: tender_db (tender-dashboard-тай ижил DB)
- **Хүснэгт**: employees, equipment, contracts, documents
- **Файл**: uploads/{employees,equipment,contracts}/UUID_filename
- **AI**: Claude sonnet-4 — шаардлагыг нөөцтэй тулгана

## Порт

- tender-dashboard: **5000**
- company-resources: **5001**
