from flask import request

class LoginForm:
    def __init__(self):
        self.email = None
        self.password = None
        self.remember = None

    def validate_on_submit(self):
        self.email = request.form.get('email')
        self.password = request.form.get('password')
        self.remember = request.form.get('remember') == 'on'
        return all([self.email, self.password])

class SetupForm:
    def __init__(self):
        self.username = None
        self.email = None
        self.password = None

    def validate_on_submit(self):
        self.username = request.form.get('username')
        self.email = request.form.get('email')
        self.password = request.form.get('password')
        return all([self.username, self.email, self.password])
