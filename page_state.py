"""Conservative visible-dialog classification; generic continue copy is not auth."""
import re
def classify_dialog(text):
    if re.search(r'verify you are human|checking your browser|unusual traffic|automated queries|access denied',text,re.I):return 'provider_blocked'
    if re.search(r'enter your email|enter your password|sign in to continue|log in to continue',text,re.I):return 'login_required'
    if re.search(r'log in|sign in|sign up',text,re.I) and re.search(r'continue with (google|apple|facebook)|password|email address',text,re.I):return 'login_required'
    if re.search(r'address|delivery|location',text,re.I):return 'address_dialog'
    return 'unknown_dialog'
