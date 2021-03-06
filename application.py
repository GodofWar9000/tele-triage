import re

import models, logging, yaml, enum, parsers, collections, threading, time, triage, matching.match_users
from flask import Flask, make_response, render_template, redirect, url_for, request, session
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from waitress import serve
import sys


application = Flask(__name__)
application.config.from_object(__name__)

# user model repo (models.py) is responsible for managing response behavior of users
user_model_repo = models.UserModelRepository(parsers.response_model_from_yaml_file('schema.yaml'), logging.getLogger(str(__name__)))
# user_queue is a dequeue of 'user' objects (each user object has phone number ('uuid') and dict w/ symptom info ('user_vals'))
user_queue = collections.deque()
# to_response_queue is a dequeue of 'user' objects who have already been triaged, but need to be matched to nearby medical centers
to_respond_queue = collections.deque()
lock = threading.RLock()
work_available_condition = threading.Condition(lock)

# configuration.yml contains:
#   Twilio credentials (acct SID, token, phone #, message service ID) 
#   Flask config variables (secret key for session, host, port, debug bool)
#   Zipcode radius (in km)
# See example_configuration.yml
config = yaml.safe_load(open('./configuration.yml'))
twilio_acct_sid = config['twilio']['acct_sid']
twilio_token = config['twilio']['token']
twilio_number = config['twilio']['number']
twilio_service_sid = config['twilio']['msg_service_sid'] # TODO: not used

application.secret_key = config['flask']['secret_key']

zip_code_radius = int(config['zipcodeapi']['radius_km'])

# Twilio client (used to send messages)
client = Client(twilio_acct_sid, twilio_token)

# home page (option to either upload/update hospital resource data or triage patients)
@application.route('/')
def home():
    return render_template('index.html')

# page for uploading/updating hospital resource data (TODO: feature not implemented yet)
@application.route('/resources')
def resources():
    return render_template('resources.html')

# page for triaging patients
@application.route('/triage')
def triaging():
    # if there are patients in the queue or the session
    # (there will be a patient in the session if they were being served by the current
    # web app user, but the web app user refreshed/left and revisted the site) 
    if len(user_queue) > 0 or ('user_uuid' in session and session['user_uuid'] != None):
        if 'user_uuid' not in session or session['user_uuid'] == None:
            user = user_queue.popleft()
            session['user_uuid'] = user.uuid
            session['user_vals'] = user.values
        # display phone number, values, and triage options (including exit, re-queuing user)
        return render_template('triage.html', values=session['user_vals'], phonenumber=session['user_uuid'])
    else:
        # TODO: continuously re-check every few seconds
        return render_template('no_users_triage.html')

# webhook for receiving & processing SMS messages 
@application.route('/sms', methods=['POST'])
def sms():
    phone_number = request.values.get('From', None)
    message_body = request.values.get('Body', None).strip()
    # if user texts 'RESTART', remove them from user model repo
    if message_body.upper() == 'RESTART':
        user_model_repo.delete(phone_number)
    resp = MessagingResponse()
    response, cont = user_model_repo.get_response(phone_number, message_body)
    resp.message(response)
    # queue user once their info has been fully collected
    if not cont:
        user_queue.append(user_model_repo.users[phone_number])
    return str(resp)

# page after the patient has been triaged 
@application.route('/verdict', methods=['POST'])
def verdict():
    # clear the session
    session['user_uuid'] = None
    session['user_vals'] = None

    triage_code = request.values.get('triages', None)
    user_number = request.values.get('phonenumber', None)
    # TODO: make these messages easier to customize

    triage_instructions, get_hospital_location = get_triage_instructions(triage_code)
    # if requeued (i.e. doctor serving them did not offer triage option)
    if triage_instructions is None:
        # todo: fix edge case of 'awkward rematching' (re-matched with same doctor who chose to requeue you)
        user_queue.appendleft(user_model_repo.users[user_number])
    else:
        # add the user to the to_respond_queue and remove them from the user model repo
        user = user_model_repo.get_or_create(user_number)
        user.values['phone_number'] = user_number
        user.values['triage_code'] = triage_code
        user.values['triage_instructions'] = triage_instructions
        user.values['get_hospital'] = get_hospital_location
        with work_available_condition:
            to_respond_queue.append(user)
            work_available_condition.notify()
        user_model_repo.delete(user_number)
    return render_template('verdict.html')

def get_triage_instructions(triage_code):
    # TODO: improve response messages
    triage_responses = {
        "home": ("Stay at home, rest, and take medication as necessary", False),
        "LEVEL 1": ("Please seek Triage Level 1 assistance; you will be provided with a list of nearby hospitals/clinics:", True),
        "LEVEL 2": ("Please seek Triage Level 2 assistance; you will be provided with a list of nearby hospitals/clinics:", True),
        "LEVEL 3": ("Please seek Triage Level 3 assistance; you will be provided with a list of nearby hospitals/clinics:", True),
        "LEVEL 4": ("Please seek Triage Level 4 assistance; you will be provided with a list of nearby hospitals/clinics:", True),
        "gettest": ("Please get tested for COVID-19; you will be provided with a list of nearby testing locations:", False),
        "checkinlater8": ("Please stay put and text back in 8 hours", False),
        "checkinlater16": ("Please stay put and text back in 16 hours", False),
        "checkinlater24": ("Please stay put and text back in 24 hours", False),
    }
    if triage_code in triage_responses:
        return triage_responses[triage_code]
    else:
        return None, None

def create_api_query_worker_thread():
    def do_work():
        while True:
            with work_available_condition:
                try:
                    user = to_respond_queue.popleft()
                    while True:  # perform API calls (may fail)
                        try:
                            instructions = user.values['triage_instructions']
                            if user.values['get_hospital']:
                                user_zip_code = user.values['zip_code']
                                hospitals = triage.get_hospital_records_within_distance(user_zip_code, zip_code_radius)
                                weights = matching.match_users.get_match_weights(user_zip_code, user.values['triage_code'],
                                                                                 hospitals)
                                selected_hospitals = triage.make_hospital_choice(hospitals, weights)
                                instructions += f'\n\nPlease choose one of the following care centers:\n '
                                for hosp in selected_hospitals:
                                    instructions += \
                                                f'{hosp["attributes"]["NAME"]} \n ' + \
                                                f'{hosp["attributes"]["ADDRESS"]} \n ' + \
                                                f'{hosp["attributes"]["CITY"]}, ' + \
                                                f'{hosp["attributes"]["STATE"]}  ' + \
                                                f'{hosp["attributes"]["ZIP"]}\n\n'
                            client.messages.create(from_=twilio_number, body=instructions, to=user.values['phone_number'])
                            break
                        except ValueError as ve:
                            print('Error: {}'.format(ve), file=sys.stderr)
                            time.sleep(1)  # sleep for one second before trying again
                        except KeyError as ke:
                            print('Value not found: {}'.format(ke.with_traceback()), file=sys.stderr)
                            break
                except:
                    work_available_condition.wait()

    return threading.Thread(target=do_work, daemon=True)

if __name__ == '__main__':
    api_threads = [create_api_query_worker_thread() for i in range(2)]
    for thr in api_threads:
        thr.start()

    if config['flask']['debug'] == True:
        application.run(host=config['flask']['host'], port=int(config['flask']['port']), debug=True)
    else:
        serve(application, host=config['flask']['host'], port=int(config['flask']['port']))