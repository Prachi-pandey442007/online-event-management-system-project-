
import os
import secrets
import uuid
from datetime import date, timedelta
from functools import wraps

import mysql.connector
import qrcode
from flask import Flask, flash, redirect, render_template, request, session, url_for
from mysql.connector import Error
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional when values are already exported
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-locally")
QR_DIRECTORY = os.path.join(app.static_folder, "generated_qr")
os.makedirs(QR_DIRECTORY, exist_ok=True)


@app.template_filter("time_display")
def time_display(value):
    """Format MySQL TIME values consistently across connector versions."""
    if value is None:
        return ""
    if isinstance(value, timedelta):
        total_minutes = int(value.total_seconds() // 60)
        hour = (total_minutes // 60) % 24
        minute = total_minutes % 60
        suffix = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        return f"{display_hour:02d}:{minute:02d} {suffix}"
    if hasattr(value, "strftime"):
        return value.strftime("%I:%M %p")
    return str(value)[:5]


def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "event_pass_db"),
    )


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in with an administrator account.", "warning")
            return redirect(url_for("login", next=request.path))
        if session.get("user_role") != "admin":
            flash("Admin access is required for that page.", "danger")
            return redirect(url_for("home"))
        return view(*args, **kwargs)

    return wrapped_view


def close_database(connection, cursor):
    if cursor:
        cursor.close()
    if connection and connection.is_connected():
        connection.close()


def event_query(where_clause="", params=()):
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    cursor.execute(
        f"""
        SELECT e.*,
               (SELECT COUNT(*) FROM registrations r
                WHERE r.event_id = e.id AND r.registration_status = 'registered') AS registration_count,
               GREATEST(e.capacity - (SELECT COUNT(*) FROM registrations r2
                WHERE r2.event_id = e.id AND r2.registration_status = 'registered'), 0) AS available_seats
        FROM events e {where_clause}
        ORDER BY e.event_date ASC, e.start_time ASC
        """,
        params,
    )
    events = cursor.fetchall()
    close_database(connection, cursor)
    return events


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        college_roll_no = request.form.get("college_roll_no", "").strip()
        password = request.form.get("password", "")

        if not full_name or not email or not college_roll_no or len(password) < 6:
            flash("Enter all required details. Password must be at least 6 characters.", "danger")
            return render_template("register.html")

        connection = cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor()
            cursor.execute(
                """INSERT INTO users
                (full_name, email, phone, college_roll_no, password_hash)
                VALUES (%s, %s, %s, %s, %s)""",
                (full_name, email, phone, college_roll_no, generate_password_hash(password)),
            )
            connection.commit()
            flash("Account created successfully. You can now log in.", "success")
            return redirect(url_for("login"))
        except Error as error:
            if error.errno == 1062:
                flash("This email is already registered.", "danger")
            else:
                flash("Registration failed. Check your MySQL connection and try again.", "danger")
        finally:
            close_database(connection, cursor)
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        connection = cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                "SELECT id, full_name, password_hash, role FROM users WHERE email = %s",
                (email,),
            )
            user = cursor.fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session.update(
                    user_id=user["id"], user_name=user["full_name"], user_role=user["role"]
                )
                next_url = request.args.get("next") or url_for("home")
                return redirect(next_url if next_url.startswith("/") else url_for("home"))
            flash("Incorrect email or password.", "danger")
        except Error:
            flash("Login failed. Check your MySQL connection and try again.", "danger")
        finally:
            close_database(connection, cursor)
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/events")
def events():
    try:
        event_list = event_query(
            "WHERE e.status = 'upcoming' AND e.event_date >= %s", (date.today(),)
        )
    except Error:
        event_list = []
        flash("Events are temporarily unavailable. Please check the database setup.", "danger")
    return render_template("events.html", events=event_list)


@app.post("/events/<int:event_id>/register")
@login_required
def register_for_event(event_id):
    connection = cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM events WHERE id = %s FOR UPDATE", (event_id,))
        event = cursor.fetchone()
        if not event:
            flash("That event could not be found.", "danger")
            return redirect(url_for("events"))
        if event["status"] != "upcoming":
            flash("Registration is closed for this event.", "warning")
            return redirect(url_for("events"))
        cursor.execute(
            "SELECT id, registration_status FROM registrations WHERE user_id = %s AND event_id = %s",
            (session["user_id"], event_id),
        )
        existing = cursor.fetchone()
        if existing and existing["registration_status"] == "registered":
            flash("You are already registered for this event.", "info")
            return redirect(url_for("my_passes"))
        cursor.execute(
            "SELECT COUNT(*) AS total FROM registrations WHERE event_id = %s AND registration_status = 'registered'",
            (event_id,),
        )
        if cursor.fetchone()["total"] >= event["capacity"]:
            flash("This event is full. No more registrations are available.", "warning")
            return redirect(url_for("events"))

        cursor.execute(
            """INSERT INTO registrations (user_id, event_id, registration_status)
            VALUES (%s, %s, 'registered')
            ON DUPLICATE KEY UPDATE registration_status = 'registered', registered_at = CURRENT_TIMESTAMP""",
            (session["user_id"], event_id),
        )
        registration_id = cursor.lastrowid
        if not registration_id:
            cursor.execute(
                "SELECT id FROM registrations WHERE user_id = %s AND event_id = %s",
                (session["user_id"], event_id),
            )
            registration_id = cursor.fetchone()["id"]
        pass_code = f"EP-{uuid.uuid4().hex[:10].upper()}"
        qr_token = secrets.token_urlsafe(32)
        cursor.execute(
            "INSERT INTO passes (registration_id, pass_code, qr_token) VALUES (%s, %s, %s)",
            (registration_id, pass_code, qr_token),
        )
        qr_path = os.path.join(QR_DIRECTORY, f"{qr_token}.png")
        qrcode.make(qr_token).save(qr_path)
        connection.commit()
        flash("Registration successful. Your digital pass is ready.", "success")
        return redirect(url_for("my_passes"))
    except Error as error:
        if connection:
            connection.rollback()
        if error.errno == 1062:
            flash("You are already registered for this event.", "info")
        else:
            flash("Registration could not be completed. Please try again.", "danger")
        return redirect(url_for("events"))
    finally:
        close_database(connection, cursor)


@app.route("/my-passes")
@login_required
def my_passes():
    connection = cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """SELECT p.*, r.registration_status, e.title, e.description, e.event_date,
            e.start_time, e.end_time, e.venue
            FROM passes p JOIN registrations r ON r.id = p.registration_id
            JOIN events e ON e.id = r.event_id
            WHERE r.user_id = %s ORDER BY e.event_date DESC, e.start_time DESC""",
            (session["user_id"],),
        )
        passes = cursor.fetchall()
    finally:
        close_database(connection, cursor)
    return render_template("my_passes.html", passes=passes)


@app.route("/admin")
@admin_required
def admin_dashboard():
    connection = cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) AS total FROM events")
        total_events = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) AS total FROM registrations WHERE registration_status = 'registered'")
        total_registrations = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) AS total FROM attendance")
        total_attendance = cursor.fetchone()["total"]
        recent_events = event_query("", ())[:5]
    except Error:
        total_events = total_registrations = total_attendance = 0
        recent_events = []
        flash("Dashboard statistics are unavailable until MySQL is connected.", "danger")
    finally:
        close_database(connection, cursor)
    return render_template(
        "admin_dashboard.html",
        total_events=total_events,
        total_registrations=total_registrations,
        total_attendance=total_attendance,
        recent_events=recent_events,
    )


def validate_event_form(form):
    title = form.get("title", "").strip()
    description = form.get("description", "").strip()
    event_date = form.get("event_date", "")
    start_time = form.get("start_time", "")
    end_time = form.get("end_time") or None
    venue = form.get("venue", "").strip()
    try:
        capacity = int(form.get("capacity", "0"))
    except ValueError:
        capacity = 0
    if not title or not event_date or not start_time or not venue or capacity < 1:
        return None
    return title, description, event_date, start_time, end_time, venue, capacity


@app.route("/admin/events")
@admin_required
def manage_events():
    try:
        event_list = event_query()
    except Error:
        event_list = []
        flash("Events could not be loaded.", "danger")
    return render_template("manage_events.html", events=event_list)


@app.route("/admin/events/create", methods=["GET", "POST"])
@admin_required
def create_event():
    if request.method == "POST":
        values = validate_event_form(request.form)
        if not values:
            flash("Complete all required fields and use a capacity of at least 1.", "danger")
            return render_template("create_event.html", event=None)
        connection = cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor()
            cursor.execute(
                """INSERT INTO events (title, description, event_date, start_time, end_time,
                venue, capacity, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (*values, session["user_id"]),
            )
            connection.commit()
            flash("Event created successfully.", "success")
            return redirect(url_for("manage_events"))
        except Error:
            if connection:
                connection.rollback()
            flash("Event could not be saved.", "danger")
        finally:
            close_database(connection, cursor)
    return render_template("create_event.html", event=None)


@app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_event(event_id):
    connection = cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM events WHERE id = %s", (event_id,))
        event = cursor.fetchone()
        if not event:
            flash("Event not found.", "danger")
            return redirect(url_for("manage_events"))
        if request.method == "POST":
            values = validate_event_form(request.form)
            if not values:
                flash("Complete all required fields and use a capacity of at least 1.", "danger")
                return render_template("create_event.html", event=event, editing=True)
            cursor.execute(
                """UPDATE events SET title=%s, description=%s, event_date=%s, start_time=%s,
                end_time=%s, venue=%s, capacity=%s, status=%s WHERE id=%s""",
                (*values, request.form.get("status", "upcoming"), event_id),
            )
            connection.commit()
            flash("Event updated successfully.", "success")
            return redirect(url_for("manage_events"))
    except Error:
        if connection:
            connection.rollback()
        flash("Event could not be updated.", "danger")
        return redirect(url_for("manage_events"))
    finally:
        close_database(connection, cursor)
    return render_template("create_event.html", event=event, editing=True)


@app.post("/admin/events/<int:event_id>/delete")
@admin_required
def delete_event(event_id):
    connection = cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            """DELETE a FROM attendance a JOIN registrations r ON r.id = a.registration_id
            WHERE r.event_id = %s""",
            (event_id,),
        )
        cursor.execute(
            "DELETE p FROM passes p JOIN registrations r ON r.id = p.registration_id WHERE r.event_id = %s",
            (event_id,),
        )
        cursor.execute("DELETE FROM registrations WHERE event_id = %s", (event_id,))
        cursor.execute("DELETE FROM events WHERE id = %s", (event_id,))
        connection.commit()
        flash("Event deleted successfully.", "success")
    except Error:
        if connection:
            connection.rollback()
        flash("Event could not be deleted.", "danger")
    finally:
        close_database(connection, cursor)
    return redirect(url_for("manage_events"))


@app.route("/admin/verify-pass", methods=["GET", "POST"])
@admin_required
def verify_pass():
    result = None
    submitted_value = ""
    if request.method == "POST":
        submitted_value = request.form.get("pass_value", "").strip()
        if not submitted_value:
            result = "invalid"
        else:
            connection = cursor = None
            try:
                connection = get_db_connection()
                cursor = connection.cursor(dictionary=True)
                cursor.execute(
                    """SELECT p.id AS pass_id, p.pass_code, p.pass_status, r.id AS registration_id,
                    r.registration_status, u.full_name, u.email, e.title, e.event_date, e.venue, e.status
                    FROM passes p JOIN registrations r ON r.id = p.registration_id
                    JOIN users u ON u.id = r.user_id JOIN events e ON e.id = r.event_id
                    WHERE p.pass_code = %s OR p.qr_token = %s""",
                    (submitted_value, submitted_value),
                )
                pass_record = cursor.fetchone()
                if not pass_record or pass_record["pass_status"] != "active" or pass_record["registration_status"] != "registered" or pass_record["status"] == "cancelled":
                    result = "invalid"
                else:
                    cursor.execute(
                        "SELECT id, check_in_time FROM attendance WHERE registration_id = %s",
                        (pass_record["registration_id"],),
                    )
                    attendance = cursor.fetchone()
                    if attendance:
                        result = "already"
                    else:
                        cursor.execute(
                            "INSERT INTO attendance (registration_id, scanned_by) VALUES (%s, %s)",
                            (pass_record["registration_id"], session["user_id"]),
                        )
                        connection.commit()
                        result = "valid"
                    submitted_value = pass_record["pass_code"]
                    pass_record["check_in_time"] = attendance["check_in_time"] if attendance else None
                    result_data = pass_record
            except Error:
                if connection:
                    connection.rollback()
                result = "error"
            finally:
                close_database(connection, cursor)
    return render_template("verify_pass.html", result=result, pass_record=locals().get("result_data"), submitted_value=submitted_value)


@app.route("/admin/reports")
@admin_required
def reports():
    selected_id = request.args.get("event_id", type=int)
    connection = cursor = None
    events_for_report = []
    report = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, title, event_date FROM events ORDER BY event_date DESC")
        events_for_report = cursor.fetchall()
        if selected_id:
            cursor.execute("SELECT * FROM events WHERE id = %s", (selected_id,))
            event = cursor.fetchone()
            if event:
                cursor.execute(
                    """SELECT u.full_name, u.email, u.college_roll_no, r.registration_status,
                    p.pass_code, p.pass_status, a.check_in_time
                    FROM registrations r JOIN users u ON u.id = r.user_id
                    LEFT JOIN passes p ON p.registration_id = r.id
                    LEFT JOIN attendance a ON a.registration_id = r.id
                    WHERE r.event_id = %s ORDER BY u.full_name""",
                    (selected_id,),
                )
                participants = cursor.fetchall()
                total_registrations = sum(1 for p in participants if p["registration_status"] == "registered")
                total_attendance = sum(1 for p in participants if p["check_in_time"])
                report = {
                    "event": event,
                    "participants": participants,
                    "total_registrations": total_registrations,
                    "total_attendance": total_attendance,
                    "absentees": max(total_registrations - total_attendance, 0),
                    "percentage": round((total_attendance / total_registrations) * 100, 1) if total_registrations else 0,
                }
    except Error:
        flash("Reports could not be loaded.", "danger")
    finally:
        close_database(connection, cursor)
    return render_template("reports.html", events=events_for_report, report=report, selected_id=selected_id)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
