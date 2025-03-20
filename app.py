from flask import Flask, request, render_template, jsonify
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime, timedelta
import json
import os
from multiprocessing import Pool
import logging
import time
import shutil
import threading
from twilio.rest import Client

app = Flask(__name__)

# הגדרת לוגים
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

CACHE_FILE = 'flight_cache.json'
CACHE_DURATION = timedelta(hours=24)
MONITORED_FLIGHTS = {}
LAST_RUN_TIME = None

# הגדרות Twilio לשליחת SMS

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
USER_PHONE_NUMBER = os.environ.get('USER_PHONE_NUMBER')
ENABLE_SMS = False

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

def setup_driver(headless=True):  # שיניתי ל-True כברירת מחדל עבור Render
    try:
        service = Service(ChromeDriverManager().install())
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36')
        driver = webdriver.Chrome(service=service, options=options)
        logger.debug("ChromeDriver נטען בהצלחה")
        return driver
    except Exception as e:
        logger.error(f"שגיאה בהגדרת ChromeDriver: {str(e)}")
        raise

def scrape_flights(args):
    date, origin, destination, direction = args
    cache_key = f"{date}_{origin}_{destination}_{direction}"
    
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            if not isinstance(cache, dict):
                logger.warning("קובץ הקאש פגום, מאתחל מחדש")
                cache = {}
            if cache_key in cache:
                if 'timestamp' in cache[cache_key] and 'data' in cache[cache_key]:
                    if (datetime.now() - datetime.strptime(cache[cache_key]['timestamp'], '%Y-%m-%d %H:%M:%S')) < CACHE_DURATION:
                        logger.debug(f"שימוש בנתונים מהקאש עבור {cache_key}")
                        return cache[cache_key]['data']
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"שגיאה בקריאת קובץ הקאש: {str(e)}, מאתחל מחדש")
            cache = {}
    
    url = f"https://www.israir.co.il/reservation/search/domestic-flights/he/results?origin={origin}&destination={destination}&startDate={date}&eilatResident=1"
    driver = setup_driver()
    try:
        logger.debug(f"מנסה לגשת לכתובת עם Selenium: {url}")
        driver.get(url)
        
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CLASS_NAME, "flight-result-item-card--domestic"))
        )
        time.sleep(3)
        
        flight_cards = driver.find_elements(By.CSS_SELECTOR, ".flight-result-item-card--domestic")
        logger.debug(f"נמצאו {len(flight_cards)} טיסות ב-HTML עם Selenium")
        
        if not flight_cards:
            logger.warning("לא נמצאו טיסות בדף")
            flight_data = [{
                'direction': direction,
                'date': date,
                'departure_time': 'אין טיסות',
                'arrival_time': 'N/A',
                'price': 'N/A',
                'seats_left': 'N/A',
                'duration': 'N/A',
                'flight_code': 'N/A',
                'airline': 'N/A',
                'origin': origin,
                'destination': destination,
                'index': 0,
                'booking_url': None,
                'last_checked': None,
                'changed': False
            }]
        else:
            flight_data = []
            for index, card in enumerate(flight_cards):
                try:
                    time_blocks = card.find_elements(By.CSS_SELECTOR, ".flight-text-block--primary .flight-text-block__bottom-text--primary")
                    departure_time = time_blocks[0].text.strip() if len(time_blocks) > 0 else "לא נמצא זמן"
                    arrival_time = time_blocks[1].text.strip() if len(time_blocks) > 1 else "לא נמצא זמן הגעה"
                    
                    price_elem = card.find_element(By.CSS_SELECTOR, ".flight-result-price__top--domestic span:last-child")
                    price = price_elem.text.strip() if price_elem else "לא נמצא מחיר"
                    
                    seats_elem = card.find_element(By.CSS_SELECTOR, ".purchase-block-button-group__top")
                    seats_left = seats_elem.text.strip() if seats_elem else "לא נמצא מידע על מקומות"
                    
                    duration_elem = card.find_element(By.CSS_SELECTOR, ".flight-text-block--sm .flight-text-block__top-text--primary")
                    duration = duration_elem.text.strip() if duration_elem else "לא נמצא משך"
                    
                    flight_code_elem = card.find_element(By.CSS_SELECTOR, ".flight-text-block__top-text--powered-by")
                    flight_code_text = flight_code_elem.text.strip()
                    flight_code = flight_code_text.split('[')[-1].split(']')[0] if '[' in flight_code_text else "לא נמצא קוד טיסה"
                    
                    airline_elem = card.find_element(By.CSS_SELECTOR, ".flight-text-block__bottom-text--powered-by .dib")
                    airline = airline_elem.text.strip() if airline_elem else "לא נמצאה חברה"
                    
                    booking_url = None
                    try:
                        select_button = card.find_element(By.CSS_SELECTOR, ".purchase-block-button-group__button")
                        button_html = driver.execute_script("return arguments[0].outerHTML;", select_button)
                        if 'data-deal-id' in button_html:
                            deal_id = select_button.get_attribute('data-deal-id')
                            booking_url = f"https://www.israir.co.il/reservation/deal/searchDomesticFlight/he/{deal_id}"
                    except NoSuchElementException:
                        pass
                    
                    flight_data.append({
                        'direction': direction,
                        'date': date,
                        'departure_time': departure_time,
                        'arrival_time': arrival_time,
                        'price': price,
                        'seats_left': seats_left,
                        'duration': duration,
                        'flight_code': flight_code,
                        'airline': airline,
                        'origin': origin,
                        'destination': destination,
                        'index': index,
                        'booking_url': booking_url,
                        'last_checked': None,
                        'changed': False
                    })
                except Exception as e:
                    logger.error(f"שגיאה בחילוץ פרטי טיסה {index}: {str(e)}")
                    continue
        
        cache = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                if not isinstance(cache, dict):
                    cache = {}
            except json.JSONDecodeError:
                cache = {}
        cache[cache_key] = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'data': flight_data
        }
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
        
        return flight_data
    
    except Exception as e:
        logger.error(f"שגיאה בגרידה עם Selenium: {str(e)}")
        return [{
            'direction': direction,
            'date': date,
            'departure_time': 'N/A',
            'arrival_time': 'N/A',
            'price': 'N/A',
            'seats_left': 'N/A',
            'duration': 'N/A',
            'flight_code': 'N/A',
            'airline': 'N/A',
            'origin': origin,
            'destination': destination,
            'index': 0,
            'booking_url': None,
            'last_checked': None,
            'changed': False
        }]
    finally:
        driver.quit()

def monitor_flights():
    global LAST_RUN_TIME
    while True:
        current_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
        if MONITORED_FLIGHTS:
            for flight_key, flight in list(MONITORED_FLIGHTS.items()):
                date, origin, destination, direction, index = flight_key.split('_')
                result = scrape_flights((date, origin, destination, direction))
                if result and len(result) > int(index):
                    current_flight = result[int(index)]
                    prev_seats = flight.get('seats_left', 'לא ידוע')
                    curr_seats = current_flight['seats_left']
                    
                    if prev_seats != curr_seats:
                        message = f"שינוי במספר המקומות בטיסה {flight['flight_code']} ב-{date} מ-{origin} ל-{destination} בשעה {flight['departure_time']}: {prev_seats} -> {curr_seats}"
                        if ENABLE_SMS:
                            send_sms(message)
                        flight['changed'] = True
                    else:
                        flight['changed'] = False
                    
                    flight['seats_left'] = curr_seats
                    flight['last_checked'] = current_time
        LAST_RUN_TIME = current_time
        time.sleep(15 * 60)

def send_sms(message):
    try:
        client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=USER_PHONE_NUMBER
        )
        logger.debug(f"SMS נשלח: {message}")
    except Exception as e:
        logger.error(f"שגיאה בשליחת SMS: {str(e)}")

@app.route('/', methods=['GET', 'POST'])
def home():
    LAST_SEARCH_FLIGHTS = getattr(app, 'last_search_flights', None)
    
    if request.method == 'POST':
        start_date = request.form['start_date']
        end_date = request.form['end_date']
        
        try:
            start = datetime.strptime(start_date, '%d/%m/%Y')
            end = datetime.strptime(end_date, '%d/%m/%Y')
            
            if start > end:
                return render_template('flights.html', error="תאריך התחלה חייב להיות לפני תאריך סיום", flights=None, last_run=LAST_RUN_TIME, monitored_flights=MONITORED_FLIGHTS)
            
            tasks = []
            current = start
            while current <= end:
                date_str = current.strftime("%d/%m/%Y")
                tasks.append((date_str, "ETM", "TLV", "הלוך"))
                tasks.append((date_str, "TLV", "ETM", "חזור"))
                current += timedelta(days=1)
            
            with Pool(processes=2) as pool:  # שיניתי ל-2 תהליכים כדי להפחית עומס ב-Render
                results = pool.map(scrape_flights, tasks)
            
            all_flights = []
            for i, result in enumerate(results):
                for flight in result:
                    flight['key'] = f"{flight['date']}_{flight['origin']}_{flight['destination']}_{flight['direction']}_{flight['index']}"
                    if flight['key'] in MONITORED_FLIGHTS:
                        monitored_flight = MONITORED_FLIGHTS[flight['key']]
                        flight['seats_left'] = monitored_flight['seats_left']
                        flight['last_checked'] = monitored_flight['last_checked']
                        flight['changed'] = monitored_flight['changed']
                    all_flights.append(flight)
            
            app.last_search_flights = all_flights
            return render_template('flights.html', flights=all_flights, error=None, last_run=LAST_RUN_TIME, monitored_flights=MONITORED_FLIGHTS)
        
        except ValueError:
            return render_template('flights.html', error="פורמט תאריך לא תקין. השתמש ב-dd/mm/yyyy", flights=LAST_SEARCH_FLIGHTS, last_run=LAST_RUN_TIME, monitored_flights=MONITORED_FLIGHTS)
    
    return render_template('flights.html', flights=LAST_SEARCH_FLIGHTS, error=None, last_run=LAST_RUN_TIME, monitored_flights=MONITORED_FLIGHTS)

@app.route('/reset_cache', methods=['POST'])
def reset_cache():
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
        return jsonify({'status': 'success', 'message': 'קובץ הקאש נמחק'})
    return jsonify({'status': 'success', 'message': 'אין קובץ קאש למחיקה'})

@app.route('/add_monitor_flight', methods=['POST'])
def add_monitor_flight():
    flight = request.get_json()
    flight_key = f"{flight['date']}_{flight['origin']}_{flight['destination']}_{flight['direction']}_{flight['index']}"
    if flight_key not in MONITORED_FLIGHTS:
        MONITORED_FLIGHTS[flight_key] = flight
        message = f"טיסה נוספה למעקב: {flight['flight_code']} ב-{flight['date']} מ-{flight['origin']} ל-{flight['destination']} בשעה {flight['departure_time']}"
        if ENABLE_SMS:
            send_sms(message)
    return jsonify({'status': 'success', 'message': 'טיסה נוספה למעקב'})

@app.route('/remove_monitor_flight', methods=['POST'])
def remove_monitor_flight():
    flight = request.get_json()
    flight_key = f"{flight['date']}_{flight['origin']}_{flight['destination']}_{flight['direction']}_{flight['index']}"
    if flight_key in MONITORED_FLIGHTS:
        del MONITORED_FLIGHTS[flight_key]
    return jsonify({'status': 'success', 'message': 'טיסה הוסרה ממעקב'})

if __name__ == '__main__':
    monitor_thread = threading.Thread(target=monitor_flights, daemon=True)
    monitor_thread.start()
    port = int(os.environ.get("PORT", 10000))  # Render משתמש ב-10000 כברירת מחדל
    app.run(host="0.0.0.0", port=port, debug=False)  # debug=False לפרודקשן