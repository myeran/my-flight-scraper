# בסיס תמונת Python מ-Render
FROM python:3.9-slim

# התקנת תלויות מערכת
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# התקנת Google Chrome
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update -y \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# הגדרת ספריית עבודה
WORKDIR /app

# העתקת קבצי הפרויקט
COPY . /app

# התקנת תלויות Python
RUN pip install --no-cache-dir -r requirements.txt

# פתיחת פורט 10000 (ברירת המחדל של Render)
EXPOSE 10000

# הגדרת פקודת הרצה
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:10000", "app:app"]