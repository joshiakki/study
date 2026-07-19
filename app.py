import os
import socket
from datetime import datetime, timedelta
import io
import base64
import smtplib
import time
import re
from functools import wraps

# --- CORRECTED STANDARD LOWER-CASE EMAIL PATHS ---
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.message import EmailMessage

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

app = Flask(__name__)
app.secret_key = "study_secret_key_pro"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Email Account Credentials Configuration ---
SENDER_EMAIL = "your_email@gmail.com"
SENDER_PASSWORD = "your_app_password"  
RECEIVER_EMAIL = "your_email@gmail.com"

# --- Database Models ---

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    
    # Establish direct one-to-many operational relationships for multi-tenant isolation
    subjects = db.relationship('Subject', backref='user', lazy=True, cascade="all, delete-orphan")
    timetable_slots = db.relationship('TimetableSlot', backref='user', lazy=True, cascade="all, delete-orphan")
    timer_state = db.relationship('ActiveTimerState', backref='user', uselist=False, cascade="all, delete-orphan")

class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False) # Dropped absolute unique=True constraint to allow different users to track same subject
    deadline = db.Column(db.Date, nullable=True) 
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    chapters = db.relationship('Chapter', backref='subject', lazy=True, cascade="all, delete-orphan")

class Chapter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    is_completed = db.Column(db.Boolean, default=False, nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)
    logs = db.relationship('StudyLog', backref='chapter', lazy=True, cascade="all, delete-orphan")
    revisions = db.relationship('RevisionSchedule', backref='chapter', lazy=True, cascade="all, delete-orphan")

class StudyLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey('chapter.id'), nullable=False)
    date = db.Column(db.Date, default=datetime.utcnow, nullable=False, index=True)
    duration_minutes = db.Column(db.Integer, nullable=False)

class RevisionSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey('chapter.id'), nullable=False)
    interval_days = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False, index=True)
    is_completed = db.Column(db.Boolean, default=False, index=True)
    confidence_rating = db.Column(db.String(20), nullable=True)

class TimetableSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day_of_week = db.Column(db.String(20), nullable=False, default="Daily")
    start_time_str = db.Column(db.String(5), nullable=False) 
    end_time_str = db.Column(db.String(5), nullable=False)   
    activity_title = db.Column(db.String(200), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class ActiveTimerState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(db.Integer, nullable=True)
    start_epoch = db.Column(db.Float, nullable=True)
    saved_ms = db.Column(db.Integer, default=0)
    is_running = db.Column(db.Boolean, default=False)
    is_completed = db.Column(db.String(5), default="no")
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
# --- Security Access Interceptor Guard Decorator ---
def login_required(f):
    """Intercepts requests and bounces unauthorized guests back to the landing portal."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("🔒 Authentication Required: Access denied. Please sign in to open your matrix panel.", "warning")
            return redirect(url_for('landing'))
        return f(*args, **kwargs)
    return decorated_function

# --- SMART DATA MIGRATION ENGINE (PROTECTS ALL EXISTING LOG HISTORY) ---
def execute_multitenant_user_migration():
    """Migrates historical logs to a core student user account without progress loss."""
    engine = db.engine
    inspector = db.inspect(engine)
    existing_tables = inspector.get_table_names()
    
    # 1. Dynamically append structural tables to support multi-device user tracking mapping
    if 'user' not in existing_tables:
        User.__table__.create(bind=engine)
        print("✓ Created authorization user tables.")
    if 'timetable_slot' not in existing_tables:
        TimetableSlot.__table__.create(bind=engine)
    if 'active_timer_state' not in existing_tables:
        ActiveTimerState.__table__.create(bind=engine)
        
    # 2. Seed your default master user tracking node parameters
    default_user = User.query.filter_by(username="default_student").first()
    if not default_user:
        print("👤 Provisioning primary multi-device student node mapping...")
        default_user = User(
            username="default_student",
            email="student@studytracker.local",
            password_hash=generate_password_hash("Default@1234")
        )
        db.session.add(default_user)
        db.session.commit()
        print("✓ Core user spawned with credentials: username 'default_student' | password 'Default@1234'")

    # 3. Dynamic layout column checks (Migrates standalone rows to the default user's ID)
    db.session.commit() # Flush transaction parameters clear
    
    # Subject table structural update checking loops
    sub_cols = [c['name'] for c in inspector.get_columns('subject')]
    if 'user_id' not in sub_cols:
        print("🔧 Re-indexing structural integrity on Subject mapping...")
        db.session.execute(db.text('ALTER TABLE subject ADD COLUMN user_id INTEGER REFERENCES user(id)'))
        db.session.commit()
        # Bind all pre-existing standalone subject entries to your core default profile
        db.session.execute(db.text(f'UPDATE subject SET user_id = {default_user.id} WHERE user_id IS NULL'))
        db.session.commit()
        
    # Timetable structural update checking loops
    time_cols = [c['name'] for c in inspector.get_columns('timetable_slot')]
    if 'user_id' not in time_cols:
        print("🔧 Re-indexing structural integrity on Timetable routing fields...")
        db.session.execute(db.text('ALTER TABLE timetable_slot ADD COLUMN user_id INTEGER REFERENCES user(id)'))
        db.session.commit()
        db.session.execute(db.text(f'UPDATE timetable_slot SET user_id = {default_user.id} WHERE user_id IS NULL'))
        db.session.commit()

    # Active timer state check loops
    timer_cols = [c['name'] for c in inspector.get_columns('active_timer_state')]
    if 'user_id' not in timer_cols:
        print("🔧 Re-indexing structural integrity on multi-device timer states...")
        db.session.execute(db.text('ALTER TABLE active_timer_state ADD COLUMN user_id INTEGER REFERENCES user(id)'))
        db.session.commit()
        db.session.execute(db.text(f'UPDATE active_timer_state SET user_id = {default_user.id} WHERE user_id IS NULL'))
        db.session.commit()

    # Spaced repetition confidence check loops
    rev_cols = [c['name'] for c in inspector.get_columns('revision_schedule')]
    if 'confidence_rating' not in rev_cols:
        db.session.execute(db.text('ALTER TABLE revision_schedule ADD COLUMN confidence_rating VARCHAR(20)'))
        db.session.commit()
        print("✓ Appended rating configuration parameters.")

# --- Traditional Analytics Core Performance Calculators ---

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_qr_base64(url_data):
    qr = qrcode.QRCode(version=1, box_size=4, border=2)
    qr.add_data(url_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

def calculate_streak(current_user_id):
    """Calculates active streak indicators isolated exclusively per user ID mapping."""
    today = datetime.utcnow().date()
    logs = db.session.query(StudyLog.date).distinct()\
        .join(Chapter, Chapter.id == StudyLog.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id)\
        .filter(Subject.user_id == current_user_id)\
        .order_by(StudyLog.date.desc()).all()
        
    logged_dates = {log.date for log in logs}
    
    streak = 0
    check_date = today
    if today not in logged_dates:
        check_date = today - timedelta(days=1)
        
    while check_date in logged_dates:
        streak += 1
        check_date -= timedelta(days=1)
    return streak
# --- ENCRYPTED ACCREDITED CROSS-DEVICE TIME SYNCHRONIZATION API ENDPOINTS ---

@app.route('/api/start_timer', methods=['POST'])
@login_required
def api_start_timer():
    """Directly saves timer state under the logged-in user's ID to sync Mobile and PC screens instantly."""
    uid = session['user_id']
    data = request.get_json() or {}
    chapter_id = data.get('chapter_id')
    live_comp_status = data.get('is_completed', 'no')
    
    if not chapter_id:
        return jsonify({"success": False, "error": "No chapter selected"}), 400
        
    # Isolate query logic exclusively to the active student session
    timer = ActiveTimerState.query.filter_by(user_id=uid).first()
    if not timer:
        timer = ActiveTimerState(user_id=uid)
        db.session.add(timer)
        
    timer.chapter_id = int(chapter_id)
    timer.is_completed = live_comp_status
    
    saved_ms = timer.saved_ms or 0
    timer.start_epoch = time.time() - (saved_ms / 1000.0)
    timer.is_running = True
    
    db.session.commit()
    return jsonify({"success": True})

@app.route('/api/pause_timer', methods=['POST'])
@login_required
def api_pause_timer():
    """Saxes accumulated elapsed time parameters straight to the user's isolated row entry."""
    uid = session['user_id']
    timer = ActiveTimerState.query.filter_by(user_id=uid).first()
    if timer and timer.is_running and timer.start_epoch:
        elapsed_seconds = time.time() - timer.start_epoch
        timer.saved_ms = int(elapsed_seconds * 1000)
    if timer:
        timer.is_running = False
    db.session.commit()
    return jsonify({"success": True})

@app.route('/api/get_timer_state')
@login_required
def api_get_timer_state():
    """Polls database rows matching the user session to synchronize separate screen views."""
    uid = session['user_id']
    timer = ActiveTimerState.query.filter_by(user_id=uid).first()
    if not timer:
        return jsonify({"running": False, "elapsed_ms": 0, "chapter_id": "", "is_completed": "no"})
        
    current_elapsed_ms = timer.saved_ms or 0
    if timer.is_running and timer.start_epoch:
        current_elapsed_ms = int((time.time() - timer.start_epoch) * 1000)
        
    return jsonify({
        "running": timer.is_running,
        "elapsed_ms": current_elapsed_ms,
        "chapter_id": str(timer.chapter_id) if timer.chapter_id else "",
        "is_completed": timer.is_completed or "no"
    })

@app.route('/api/clear_timer_state', methods=['POST'])
@login_required
def api_clear_timer_state():
    """Purges tracking state properties after a recording block has successfully posted."""
    uid = session['user_id']
    timer = ActiveTimerState.query.filter_by(user_id=uid).first()
    if timer:
        timer.chapter_id = None
        timer.start_epoch = None
        timer.saved_ms = 0
        timer.is_running = False
        timer.is_completed = "no"
        db.session.commit()
    return jsonify({"success": True})

# --- TIME-FILTER GRAPH ANALYTICS CONTROLLERS ---

@app.route('/api/timeline/<string:mode>')
@login_required
def get_timeline_data(mode):
    uid = session['user_id']
    today = datetime.utcnow().date()
    
    if mode == 'daily':
        date_targets = [today]
        labels = ["Today"]
    elif mode == 'monthly':
        date_targets = [today - timedelta(days=i) for i in range(29, -1, -1)]
        labels = [d.strftime('%d-%b') for d in date_targets]
    else:
        date_targets = [today - timedelta(days=i) for i in range(6, -1, -1)]
        labels = [d.strftime('%a') for d in date_targets]

    chart_bars = []
    for i, target_date in enumerate(date_targets):
        total_minutes = db.session.query(db.func.sum(StudyLog.duration_minutes))\
            .join(Chapter, Chapter.id == StudyLog.chapter_id)\
            .join(Subject, Subject.id == Chapter.subject_id)\
            .filter(Subject.user_id == uid, StudyLog.date == target_date).scalar() or 0
        chart_bars.append({"label": labels[i], "mins": total_minutes, "date_str": target_date.strftime('%Y-%m-%d')})

    max_minutes = max([item["mins"] for item in chart_bars]) if chart_bars else 1
    max_minutes = max_minutes if max_minutes > 0 else 1
    for item in chart_bars:
        item["height_pct"] = int((item["mins"] / max_minutes) * 100)

    return jsonify({"chart_bars": chart_bars})

@app.route('/api/timeline_details/<string:date_str>')
@login_required
def get_timeline_details(date_str):
    uid = session['user_id']
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    logs = StudyLog.query.join(Chapter, Chapter.id == StudyLog.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id)\
        .filter(Subject.user_id == uid, StudyLog.date == target_date).all()
        
    sessions = []
    for l in logs:
        sessions.append({"type": "Study Session", "subject": l.chapter.subject.name, "chapter": l.chapter.name, "duration": f"{l.duration_minutes} mins", "rating": "N/A"})

    revisions = RevisionSchedule.query.join(Chapter, Chapter.id == RevisionSchedule.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id)\
        .filter(Subject.user_id == uid, RevisionSchedule.due_date == target_date, RevisionSchedule.is_completed == True).all()
        
    for r in revisions:
        sessions.append({"type": f"Revision (Day {r.interval_days})", "subject": r.chapter.subject.name, "chapter": r.chapter.name, "duration": "Completed Check", "rating": r.confidence_rating if r.confidence_rating else "Not Rated"})

    return jsonify({"date": target_date.strftime('%d-%b-%Y'), "sessions": sessions})
# --- MARKETING SHOWCASE LANDING & AUTHENTICATION SYSTEMS ---

@app.route('/welcome')
def landing():
    """Renders the comprehensive marketing landing showcase platform profile window."""
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def auth_login():
    """Handles secure validation queries mapping hashed security password credentials."""
    if 'user_id' in session:
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            flash(f"🎉 Welcome back, {user.username}! Let's hit today's preparation goals.", "warning")
            return redirect(url_for('index'))
        else:
            flash("❌ Access Denied: Invalid credentials pattern matching. Check caps lock or password strings.", "warning")
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def auth_register():
    """Registers fresh multi-tenant student profiles securely inside isolated schemas."""
    if 'user_id' in session:
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if password != confirm_password:
            flash("⚠️ Verification Failure: Input password fields do not match structural patterns.", "warning")
            return render_template('register.html')
            
        if User.query.filter_by(username=username).first():
            flash("⚠️ Account Conflict: That student username signature is already occupied on this router.", "warning")
            return render_template('register.html')
            
        if User.query.filter_by(email=email).first():
            flash("⚠️ Account Conflict: A student profile mapping already links to this email contact address.", "warning")
            return render_template('register.html')
            
        hashed_value = generate_password_hash(password)
        new_user = User(username=username, email=email, password_hash=hashed_value)
        db.session.add(new_user)
        db.session.commit()
        
        # Provision an explicit empty synchronized live stopwatch state structure matrix row node entry instantly
        db.session.add(ActiveTimerState(user_id=new_user.id))
        db.session.commit()
        
        flash("🎉 Registration Completed! Secure profile initialized. Please log in to connect.", "warning")
        return redirect(url_for('auth_login'))
        
    # FIXED: Added explicit fallback return to display the registration form on GET requests
    return render_template('register.html')


@app.route('/forgot_password', methods=['GET', 'POST'])
def auth_forgot_password():
    """Handles password reset simulation maps securely verifying root registrations."""
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        new_password = request.form.get('new_password', '')
        
        user = User.query.filter_by(email=email).first()
        if user:
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("🔒 Password security string updated cleanly! Please log in with your updated phrase.", "warning")
            return redirect(url_for('auth_login'))
        else:
            flash("⚠️ Identity Mismatch: No user profile accounts matched this email query registration signature.", "warning")
            
    return render_template('forgot_password.html')

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def account_settings():
    """Handles secure profile information updates and password changes for logged-in users."""
    uid = session['user_id']
    user = User.query.get_or_404(uid)
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'update_profile':
            new_username = request.form.get('username', '').strip()
            new_email = request.form.get('email', '').strip()
            current_password = request.form.get('current_password', '')
            
            # 1. Verify credentials before making profile mutations
            if not check_password_hash(user.password_hash, current_password):
                flash("❌ Access Denied: Incorrect current password confirmation.", "warning")
                return redirect(url_for('account_settings'))
                
            if not new_username or not new_email:
                flash("⚠️ Configuration Error: Username and Email fields cannot be blank.", "warning")
                return redirect(url_for('account_settings'))
                
            # 2. Check for username/email conflicts excluding the current user
            username_exists = User.query.filter(User.username == new_username, User.id != uid).first()
            if username_exists:
                flash("⚠️ Account Conflict: That username signature is already occupied on this router.", "warning")
                return redirect(url_for('account_settings'))
                
            email_exists = User.query.filter(User.email == new_email, User.id != uid).first()
            if email_exists:
                flash("⚠️ Account Conflict: Another student profile already links to this email contact address.", "warning")
                return redirect(url_for('account_settings'))
                
            # 3. Apply profile updates safely
            user.username = new_username
            user.email = new_email
            db.session.commit()
            
            # Sync the session memory to immediately update navigation bar badges
            session['username'] = user.username
            flash("✓ Profile settings updated successfully!", "warning")
            
        elif action == 'change_password':
            old_password = request.form.get('old_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            
            # 1. Verify current credential hash match
            if not check_password_hash(user.password_hash, old_password):
                flash("❌ Access Denied: Incorrect current password confirmation.", "warning")
                return redirect(url_for('account_settings'))
                
            # 2. Match confirmation rules
            if new_password != confirm_password:
                flash("⚠️ Verification Failure: New password fields do not match structural patterns.", "warning")
                return redirect(url_for('account_settings'))
                
            if len(new_password) < 6:
                flash("⚠️ Security Restriction: New password must be at least 6 characters long.", "warning")
                return redirect(url_for('account_settings'))
                
            # 3. Encrypt and apply the new hash token safely
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("🔒 Password security string overwritten cleanly! Please use your updated phrase on next sign-in.", "warning")
            
        return redirect(url_for('account_settings'))
        
    return render_template('settings.html', user=user)


@app.route('/logout')
def auth_logout():
    """Terminates active authenticated network matrix ports session memory handles instantly."""
    session.pop('user_id', None)
    session.pop('username', None)
    flash("🔒 Session Terminal Disconnected. Private preparation logs secured.", "warning")
    return redirect(url_for('landing'))
# --- PRIMARY ISOLATED DASHBOARD MANAGEMENT LAYER ---

@app.route('/')
@login_required
def index():
    uid = session['user_id']
    today = datetime.utcnow().date()
    days_list = [today - timedelta(days=i) for i in range(6, -1, -1)]
    
    # Generate 7-Day History Chart Data baseline isolated by active user ID mapping
    weekly_data = []
    for d in days_list:
        total_min = db.session.query(db.func.sum(StudyLog.duration_minutes))\
            .join(Chapter, Chapter.id == StudyLog.chapter_id)\
            .join(Subject, Subject.id == Chapter.subject_id)\
            .filter(Subject.user_id == uid, StudyLog.date == d).scalar() or 0
        weekly_data.append({"day_name": d.strftime('%a'), "mins": total_min, "date_str": d.strftime('%Y-%m-%d')})
        
    max_weekly = max([x["mins"] for x in weekly_data]) if weekly_data else 1
    max_weekly = max_weekly if max_weekly > 0 else 1
    for x in weekly_data:
        x["height_pct"] = int((x["mins"] / max_weekly) * 100)

    # Time Distribution Breakdown per Subject (FIXED: Unpacks Row object tuple indices safely)
    time_records = db.session.query(Subject.name, db.func.sum(StudyLog.duration_minutes))\
        .join(Chapter, Chapter.subject_id == Subject.id).join(StudyLog, StudyLog.chapter_id == Chapter.id)\
        .filter(Subject.user_id == uid).group_by(Subject.name).all()
    
    # row[0] is the Subject Name string, row[1] is the Integer minutes count
    time_list = [{"subject": row[0], "minutes": int(row[1] if row[1] is not None else 0)} for row in time_records]
    max_minutes = max([item["minutes"] for item in time_list]) if time_list else 1
    for item in time_list:
        item["pct"] = int((item["minutes"] / max_minutes) * 100)

    subjects = Subject.query.filter_by(user_id=uid).all()
    progress_list = []
    total_chapters_all = 0
    completed_chapters_all = 0
    total_subjects_count = len(subjects)
    completed_subjects_count = 0

    for sub in subjects:
        total = len(sub.chapters)
        completed = sum(1 for c in sub.chapters if c.is_completed)
        total_chapters_all += total
        completed_chapters_all += completed
        
        if total > 0 and completed == total:
            completed_subjects_count += 1

        progress_pct = int((completed / total) * 100) if total > 0 else 0
        days_left = None
        if sub.deadline:
            delta = sub.deadline - today
            days_left = delta.days

        progress_list.append({
            "subject_name": sub.name, "total": total, "completed": completed,
            "leftover": total - completed, "pct": progress_pct,
            "deadline": sub.deadline.strftime('%d-%b-%Y') if sub.deadline else "No Target Defined",
            "days_left": days_left
        })

    course_completion_pct = int((completed_chapters_all / total_chapters_all) * 100) if total_chapters_all > 0 else 0
    subjects_leftover_count = total_subjects_count - completed_subjects_count

    total_revisions_logged = RevisionSchedule.query.join(Chapter, Chapter.id == RevisionSchedule.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id).filter(Subject.user_id == uid).count()
        
    completed_revisions_count = RevisionSchedule.query.join(Chapter, Chapter.id == RevisionSchedule.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id).filter(Subject.user_id == uid, RevisionSchedule.is_completed == True).count()
        
    revision_completion_pct = int((completed_revisions_count / total_revisions_logged) * 100) if total_revisions_logged > 0 else 0
    pending_revisions_leftover = total_revisions_logged - completed_revisions_count

    local_url = f"http://{get_local_ip()}:5000"
    qr_b64 = get_qr_base64(local_url)

    due_count = RevisionSchedule.query.join(Chapter, Chapter.id == RevisionSchedule.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id)\
        .filter(Subject.user_id == uid, RevisionSchedule.due_date <= today, RevisionSchedule.is_completed == False).count()
        
    if due_count > 0:
        flash(f"🔔 Target Alert Popup: You have {due_count} topic reviews scheduled to process today!", "warning")

    current_routine = get_current_routine_activity_for_user(uid)

    # Pull user isolated universal timer state records
    timer_state = ActiveTimerState.query.filter_by(user_id=uid).first()
    is_timer_active = timer_state.is_running if timer_state else False

    return render_template(
        'index.html', time_list=time_list, progress_list=progress_list,
        subjects=subjects, today_str=today.strftime('%Y-%m-%d'),
        streak=calculate_streak(uid), weekly_data=weekly_data, qr_b64=qr_b64, local_url=local_url,
        course_completion_pct=course_completion_pct, subjects_leftover_count=subjects_leftover_count,
        total_subjects_count=total_subjects_count, revision_completion_pct=revision_completion_pct,
        pending_revisions_leftover=pending_revisions_leftover, total_revisions_logged=total_revisions_logged,
        current_routine=current_routine, is_timer_active=is_timer_active
    )

def get_current_routine_activity_for_user(user_id):
    now = datetime.now()
    current_str = now.strftime("%H:%M")
    active_slot = TimetableSlot.query.filter(
        TimetableSlot.user_id == user_id,
        TimetableSlot.start_time_str <= current_str,
        TimetableSlot.end_time_str >= current_str
    ).first()
    if active_slot:
        return {"title": active_slot.activity_title, "time": f"{active_slot.start_time_str} - {active_slot.end_time_str}"}
    return {"title": "🎯 Free Block / Open Study Window", "time": "No routine task matched to this hour"}

@app.route('/trigger_email')
@login_required
def email_trigger_route():
    success, feedback_msg = send_daily_summary_email_isolated(session['user_id'])
    flash(feedback_msg, "warning")
    return redirect(url_for('index'))


@app.route('/log_study', methods=['POST'])
@login_required
def log_study():
    uid = session['user_id']
    chapter_id = request.form.get('chapter_id')
    duration = int(request.form.get('duration', 0))
    date_str = request.form.get('date')
    is_completed_form = request.form.get('is_completed')
    
    study_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.utcnow().date()

    if chapter_id and duration > 0:
        # Verify the target chapter belongs to a subject owned by this user
        chapter_record = Chapter.query.get_or_404(chapter_id)
        if chapter_record.subject.user_id != uid:
            flash("⚠️ Security Intercept: Unauthorized operation.", "warning")
            return redirect(url_for('index'))

        # GUARD 1: Block manual inputs if a stopwatch is currently running for this user
        timer_state = ActiveTimerState.query.filter_by(user_id=uid).first()
        if timer_state and timer_state.is_running:
            flash("⚠️ Entry Rejected: A live stopwatch is currently running on another device. Stop the timer first!", "warning")
            return redirect(url_for('index'))

        # GUARD 2: Enforce the 24-hour daily total limit rule (1440 minutes max per day per account)
        existing_day_minutes = db.session.query(db.func.sum(StudyLog.duration_minutes))\
            .join(Chapter, Chapter.id == StudyLog.chapter_id)\
            .join(Subject, Subject.id == Chapter.subject_id)\
            .filter(Subject.user_id == uid, StudyLog.date == study_date).scalar() or 0
            
        if existing_day_minutes + duration > 1440:
            flash(f"⚠️ Entry Rejected: Cannot log {duration} mins. Your total study time for this single day would exceed the 24-hour limit ({existing_day_minutes}/1440 mins logged)!", "warning")
            return redirect(url_for('index'))

        db.session.add(StudyLog(chapter_id=chapter_id, date=study_date, duration_minutes=duration))
        chapter_record.is_completed = True if is_completed_form == 'yes' else False
        
        intervals = [1, 3, 7, 21]
        for days in intervals:
            due = study_date + timedelta(days=days)
            exists = RevisionSchedule.query.filter_by(chapter_id=chapter_id, interval_days=days, is_completed=False).first()
            if not exists:
                db.session.add(RevisionSchedule(chapter_id=chapter_id, interval_days=days, due_date=due))
        db.session.commit()
    return redirect(url_for('index'))

def send_daily_summary_email_isolated(user_id):
    """Compiles tasks and dispatches mail matching only the target user's records."""
    today = datetime.utcnow().date()
    user_record = User.query.get(user_id)
    if not user_record or not user_record.email:
        return False, "⚠️ Mail Error: No valid profile email destination registered."

    subjects = Subject.query.filter_by(user_id=user_id).all()
    todo_chapters = []
    for sub in subjects:
        days_status = ""
        if sub.deadline:
            delta = sub.deadline - today
            if delta.days < 0:
                days_status = f" (OVERDUE by {abs(delta.days)} days!)"
            elif delta.days == 0:
                days_status = " (SUBJECT TARGET DUE TODAY!)"
            else:
                days_status = f" ({delta.days} days remaining)"

        for chap in sub.chapters:
            if not chap.is_completed:
                todo_chapters.append(f"• ❌ [Leftover] {sub.name}: {chap.name}{days_status}")

    due_revisions = RevisionSchedule.query.join(Chapter, Chapter.id == RevisionSchedule.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id)\
        .filter(Subject.user_id == user_id, RevisionSchedule.due_date <= today, RevisionSchedule.is_completed == False).all()
    
    todo_revisions = []
    for rev in due_revisions:
        todo_revisions.append(f"• ⏳ [Revision Day {rev.interval_days}] {rev.chapter.subject.name}: {rev.chapter.name}")

    subject_line = f"📚 Study Matrix Task Digest: {today.strftime('%d-%b-%Y')}"
    body = f"Hello {user_record.username}! Here is your personalized task summary for today:\n\n"
    body += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    body += "🔥 CURRENT UNFINISHED SYLLABUS CHAPTERS\n"
    body += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    body += "\n".join(todo_chapters) if todo_chapters else "🎉 Amazing! You have checked off 100% of your course syllabus chapters."
        
    body += "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    body += "⏳ SPACED REPETITION REVISIONS DUE TODAY\n"
    body += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    body += "\n".join(todo_revisions) if todo_revisions else "🎉 Clear schedule! No retention reviews are pending today."

    try:
        msg = EmailMessage()
        msg["Subject"] = subject_line
        msg["From"] = SENDER_EMAIL
        msg["To"] = user_record.email
        msg.set_content(body)

        with smtplib.SMTP("://gmail.com", 587) as server:
            server.starttls()  
            server.login(SENDER_EMAIL, SENDER_PASSWORD) 
            server.send_message(msg)
        return True, "✓ Digest dispatched! Check your email inbox."
    except Exception as error:
        return False, f"⚠️ Mail Connection Error: {str(error)}"

@app.route('/revisions')
@login_required
def view_revisions():
    uid = session['user_id']
    today = datetime.utcnow().date()
    
    due = RevisionSchedule.query.join(Chapter, Chapter.id == RevisionSchedule.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id)\
        .filter(Subject.user_id == uid, RevisionSchedule.due_date <= today, RevisionSchedule.is_completed == False).all()
        
    upcoming = RevisionSchedule.query.join(Chapter, Chapter.id == RevisionSchedule.chapter_id)\
        .join(Subject, Subject.id == Chapter.subject_id)\
        .filter(Subject.user_id == uid, RevisionSchedule.due_date > today, RevisionSchedule.is_completed == False)\
        .order_by(RevisionSchedule.due_date.asc()).all()
        
    return render_template('revisions.html', due=due, upcoming=upcoming)

@app.route('/complete_revision/<int:id>', methods=['POST'])
@login_required
def complete_revision(id):
    uid = session['user_id']
    rev = RevisionSchedule.query.get_or_404(id)
    if rev.chapter.subject.user_id != uid:
        return "Unauthorized", 403
        
    quality = request.form.get('quality')
    today = datetime.utcnow().date()
    
    rev.is_completed = True
    rev.confidence_rating = quality.upper()
    
    if quality == 'hard':
        new_due = today + timedelta(days=3)
        db.session.add(RevisionSchedule(chapter_id=rev.chapter_id, interval_days=3, due_date=new_due))
    elif quality == 'medium':
        new_due = today + timedelta(days=7)
        db.session.add(RevisionSchedule(chapter_id=rev.chapter_id, interval_days=7, due_date=new_due))
    elif quality == 'easy':
        new_due = today + timedelta(days=20)
        db.session.add(RevisionSchedule(chapter_id=rev.chapter_id, interval_days=20, due_date=new_due))
    db.session.commit()
    return redirect(url_for('view_revisions'))
@app.route('/subjects', methods=['GET', 'POST'])
@login_required
def manage_subjects():
    uid = session['user_id']
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_subject':
            name = request.form.get('subject_name', '').strip()
            deadline_str = request.form.get('subject_deadline')
            deadline_date = datetime.strptime(deadline_str, '%Y-%m-%d').date() if deadline_str else None
            
            if name:
                existing_subject = Subject.query.filter_by(name=name, user_id=uid).first()
                if existing_subject:
                    flash(f"⚠️ Error: A subject named '{name}' already exists in your curriculum profile!", "warning")
                else:
                    db.session.add(Subject(name=name, deadline=deadline_date, user_id=uid))
                    db.session.commit()
        
        elif action == 'edit_subject':
            sub_id = request.form.get('subject_id')
            new_name = request.form.get('subject_name', '').strip()
            new_deadline_str = request.form.get('subject_deadline')
            new_deadline_date = datetime.strptime(new_deadline_str, '%Y-%m-%d').date() if new_deadline_str else None
            
            if sub_id and new_name:
                sub = Subject.query.get_or_404(sub_id)
                if sub.user_id != uid:
                    return "Unauthorized", 403
                existing_subject = Subject.query.filter(Subject.name == new_name, Subject.user_id == uid, Subject.id != sub.id).first()
                if existing_subject:
                    flash(f"⚠️ Error: Another subject named '{new_name}' already exists in your curriculum profile!", "warning")
                else:
                    sub.name = new_name
                    sub.deadline = new_deadline_date
                    db.session.commit()
                    
        elif action == 'add_chapter':
            sub_id = request.form.get('subject_id')
            chap_name = request.form.get('chapter_name', '').strip()
            if sub_id and chap_name:
                sub = Subject.query.get_or_404(sub_id)
                if sub.user_id != uid:
                    return "Unauthorized", 403
                exists = Chapter.query.filter_by(name=chap_name, subject_id=sub_id).first()
                if exists:
                    flash(f"⚠️ Error: Chapter '{chap_name}' already exists in this subject!", "warning")
                else:
                    db.session.add(Chapter(name=chap_name, subject_id=sub_id))
                    db.session.commit()
                    
        elif action == 'bulk_upload':
            bulk_text = request.form.get('bulk_data', '').strip()
            default_deadline_str = request.form.get('bulk_deadline')
            default_deadline = datetime.strptime(default_deadline_str, '%Y-%m-%d').date() if default_deadline_str else None
            
            if bulk_text:
                lines = bulk_text.split('\n')
                added_subjects = 0
                added_chapters = 0
                
                for line in lines:
                    if ':' in line:
                        sub_part, chaps_part = line.split(':', 1)
                        sub_name = sub_part.strip()
                        if sub_name:
                            subject = Subject.query.filter_by(name=sub_name, user_id=uid).first()
                            if not subject:
                                subject = Subject(name=sub_name, deadline=default_deadline, user_id=uid)
                                db.session.add(subject)
                                db.session.commit()
                                added_subjects += 1
                            
                            chapters_list = [c.strip() for c in chaps_part.split(',') if c.strip()]
                            for chap_name in chapters_list:
                                exists = Chapter.query.filter_by(name=chap_name, subject_id=subject.id).first()
                                if not exists:
                                    db.session.add(Chapter(name=chap_name, subject_id=subject.id))
                                    added_chapters += 1
                db.session.commit()
                flash(f"✓ Bulk Import Success! Added {added_subjects} new subjects and {added_chapters} chapters.", "warning")
                    
        elif action == 'edit_chapter':
            chap_id = request.form.get('chapter_id')
            new_name = request.form.get('chapter_name', '').strip()
            if chap_id and new_name:
                chap = Chapter.query.get_or_404(chap_id)
                if chap.subject.user_id != uid:
                    return "Unauthorized", 403
                exists = Chapter.query.filter(Chapter.name == new_name, Chapter.subject_id == chap.subject_id, Chapter.id != chap.id).first()
                if exists:
                    flash(f"⚠️ Error: Another chapter named '{new_name}' already exists in this subject!", "warning")
                else:
                    chap.name = new_name
                    db.session.commit()
                    flash(f"✓ Chapter updated successfully!", "warning")
        return redirect(url_for('manage_subjects'))
    return render_template('subjects.html', subjects=Subject.query.filter_by(user_id=uid).all())

@app.route('/timetable', methods=['GET', 'POST'])
@login_required
def manage_timetable():
    uid = session['user_id']
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_slot':
            start = request.form.get('start_time', '').strip()
            end = request.form.get('end_time', '').strip()
            title = request.form.get('activity_title', '').strip()
            if start and end and title:
                if start >= end:
                    flash("⚠️ Configuration Error: Start time must be strictly before end time!", "warning")
                else:
                    db.session.add(TimetableSlot(start_time_str=start, end_time_str=end, activity_title=title, user_id=uid))
                    db.session.commit()
                    flash(f"✓ Added routine entry successfully.", "warning")
        elif action == 'bulk_timetable_upload':
            bulk_text = request.form.get('bulk_timetable_data', '').strip()
            if bulk_text:
                lines = bulk_text.split('\n')
                added_slots = 0
                for line in lines:
                    line = line.strip()
                    if not line or line.lower().startswith('time'):
                        continue
                    time_match = re.match(r'^(\d{1,2}:\d{2})\s*[–\-\—]\s*(\d{1,2}:\d{2})\s*(AM|PM)?', line, re.IGNORECASE)
                    if time_match:
                        raw_start = time_match.group(1)
                        raw_end = time_match.group(2)
                        period = time_match.group(3)
                        remaining_text = line[time_match.end():].strip()
                        parts = re.split(r'\t+|\s{2,}', remaining_text)
                        activity_title = parts[0].strip() if len(parts) > 1 else parts[0].strip()
                        if activity_title:
                            try:
                                def convert_to_24h(time_str, default_period):
                                    t_obj = datetime.strptime(time_str.strip(), "%H:%M")
                                    p = default_period if default_period else ("PM" if t_obj.hour >= 12 or t_obj.hour < 5 else "AM")
                                    if p and p.upper() == "PM" and t_obj.hour < 12:
                                        t_obj = t_obj.replace(hour=t_obj.hour + 12)
                                    if p and p.upper() == "AM" and t_obj.hour == 12:
                                        t_obj = t_obj.replace(hour=0)
                                    return t_obj.strftime("%H:%M")
                                start_24 = convert_to_24h(raw_start, period)
                                end_24 = convert_to_24h(raw_end, period)
                                db.session.add(TimetableSlot(start_time_str=start_24, end_time_str=end_24, activity_title=activity_title, user_id=uid))
                                added_slots += 1
                            except Exception:
                                continue
                db.session.commit()
                flash(f"✓ Bulk Routine Import Success! Parsed and loaded {added_slots} schedule blocks.", "warning")
        elif action == 'clear_timetable':
            db.session.query(TimetableSlot).filter_by(user_id=uid).delete()
            db.session.commit()
            flash("✓ Daily routine timetable cleared successfully.", "warning")
        return redirect(url_for('manage_timetable'))
        
    slots = TimetableSlot.query.filter_by(user_id=uid).order_by(TimetableSlot.start_time_str.asc()).all()
    return render_template('timetable.html', slots=slots)

@app.route('/delete_chapter/<int:id>')
@login_required
def delete_chapter(id):
    uid = session['user_id']
    chap = Chapter.query.get_or_404(id)
    if chap.subject.user_id != uid:
        return "Unauthorized", 403
    db.session.delete(chap)
    db.session.commit()
    flash("✓ Chapter removed from curriculum maps.", "warning")
    return redirect(url_for('manage_subjects'))

@app.route('/delete_subject/<int:id>')
@login_required
def delete_subject(id):
    uid = session['user_id']
    sub = Subject.query.get_or_404(id)
    if sub.user_id != uid:
        return "Unauthorized", 403
    db.session.delete(sub)
    db.session.commit()
    flash(f"✓ Subject '{sub.name}' and all its data have been removed.", "warning")
    return redirect(url_for('manage_subjects'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        execute_multitenant_user_migration() # Protects and links logs securely
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
