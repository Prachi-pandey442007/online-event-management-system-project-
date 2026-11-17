from flask import Flask, render_template, request, redirect, url_for, flash
import mysql.connector
from werkzeug.security import generate_password_hash
from mysql.connector import Error

app = Flask(__name__)
app.secret_key = "event-pass-development-key"
def get_db_connection():
    connection = mysql.connector.connect(
        host="localhost",
        user="root",
        password="password",
        database="event_pass_db"
    )
    return connection


@app.route("/")
def home():
    return render_template("home.html")


# This part is newly added for register.html
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form["full_name"].strip()
        email = request.form["email"].strip()
        phone = request.form.get("phone", "").strip()
        college_roll_no = request.form["college_roll_no"].strip()
        password = request.form["password"]

        password_hash = generate_password_hash(password)

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            query = """
                INSERT INTO users
                (full_name, email, phone, college_roll_no, password_hash)
                VALUES (%s, %s, %s, %s, %s)
            """

            values = (
                full_name,
                email,
                phone,
                college_roll_no,
                password_hash
            )

            cursor.execute(query, values)
            connection.commit()

            flash("Account created successfully.", "success")
            return redirect(url_for("register"))

        except Error as error:
            if error.errno == 1062:
                flash("This email is already registered.", "danger")
            else:
                flash("Registration failed. Please try again.", "danger")

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

    return render_template("register.html")