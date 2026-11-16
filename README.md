# Lup
LuP - العربي للعربي

## التشغيل محلياً
```bash
pip install -r requirements.txt
cp .env.example .env   # ثم املأ المفاتيح
export $(grep -v '^#' .env | xargs)
python3 podlup.py      # http://localhost:8000
```

## الاختبارات
```bash
pip install -r requirements-dev.txt
pytest
```
