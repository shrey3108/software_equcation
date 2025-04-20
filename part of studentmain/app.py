import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_pymongo import PyMongo
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from bson import ObjectId
from pymongo import MongoClient
from functools import wraps
import json
import gridfs
import io
from config import GEMINI_API_KEY, MONGO_URI, SECRET_KEY, YOUTUBE_API_KEY
import google.generativeai as genai
import requests
from googleapiclient.discovery import build
import urllib.parse
from collections import defaultdict
import pandas as pd
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'  # Change this to a secure secret key
app.config['MONGO_URI'] = MONGO_URI
app.config['GEMINI_API_KEY'] = GEMINI_API_KEY
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Create uploads directory if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'submissions'), exist_ok=True)

# Configure Gemini AI
genai.configure(api_key=app.config['GEMINI_API_KEY'])

# YouTube API Configuration

mongo = PyMongo(app)
fs = gridfs.GridFS(mongo.db)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

class User(UserMixin):
    def __init__(self, user_data):
        self.user_data = user_data
        self.id = str(user_data['_id'])
        self.username = user_data['username']
        self.role = user_data['role']

    @staticmethod
    def get(user_id):
        user_data = mongo.db.users.find_one({'_id': ObjectId(user_id)})
        return User(user_data) if user_data else None

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)



@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        email = request.form['email']
        role = request.form['role']

        if mongo.db.users.find_one({'username': username}):
            flash('Username already exists')
            return redirect(url_for('register'))

        # Common user data
        user_data = {
            'username': username,
            'password_hash': generate_password_hash(password),
            'email': email,
            'role': role,
            'full_name': request.form['full_name'],
            'phone': request.form['phone'],
            'date_of_birth': request.form['date_of_birth'],
            'created_at': datetime.utcnow(),
            'last_login': datetime.utcnow()
        }

        # Add role-specific data
        if role == 'student':
            user_data.update({
                'grade_level': request.form.get('grade_level'),
                'parent_name': request.form.get('parent_name'),
                'parent_phone': request.form.get('parent_phone')
            })
        else:  # teacher
            user_data.update({
                'qualifications': request.form.get('qualifications'),
                'specialization': request.form.get('specialization'),
                'experience_years': request.form.get('experience_years')
            })

        mongo.db.users.insert_one(user_data)
        flash('Registration successful!')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user_data = mongo.db.users.find_one({'username': username})

        if user_data and check_password_hash(user_data['password_hash'], password):
            user = User(user_data)
            login_user(user)
            if user.role == 'student':
                return redirect(url_for('student_dashboard'))
            else:
                return redirect(url_for('teacher_dashboard'))

        flash('Invalid username or password')
    return render_template('login.html')

@app.route('/student_dashboard')
@login_required
def student_dashboard():
    if current_user.role != 'student':
        flash('Access denied. Student privileges required.', 'danger')
        return redirect(url_for('index'))
    
    # Get all subjects where student is enrolled
    enrolled_subjects = list(mongo.db.subjects.find({
        'enrollments.student_id': ObjectId(current_user.id)
    }))
    
    # Calculate progress for each subject and overall stats
    total_assignments = 0
    completed_assignments = 0
    upcoming_assignments = []
    current_time = datetime.utcnow()
    
    for subject in enrolled_subjects:
        subject_assignments = len(subject.get('assignments', []))
        subject_completed = 0
        
        for assignment in subject.get('assignments', []):
            total_assignments += 1
            is_completed = False
            grade = None
            feedback = None
            
            # Check if assignment is completed and get grade
            for submission in assignment.get('submissions', []):
                if str(submission.get('student_id')) == str(current_user.id):
                    subject_completed += 1
                    completed_assignments += 1
                    is_completed = True
                    grade = submission.get('grade')
                    feedback = submission.get('feedback')
                    break  # Count only one submission per assignment
            
            # If assignment is not completed and has a future due date, add to upcoming
            if not is_completed and assignment.get('due_date'):
                try:
                    due_date = assignment['due_date']
                    if isinstance(due_date, datetime) and due_date > current_time:
                        upcoming_assignments.append({
                            '_id': assignment['_id'],
                            'title': assignment.get('title', 'Untitled Assignment'),
                            'subject_name': subject.get('name', 'Unknown Subject'),
                            'due_date': due_date,
                            'grade': grade,
                            'feedback': feedback
                        })
                except Exception as e:
                    print(f"Debug: Error processing assignment due date: {str(e)}")
        
        # Calculate and set progress percentage for this subject
        subject['progress'] = round((subject_completed / subject_assignments * 100) if subject_assignments > 0 else 0)
    
    # Calculate overall progress
    overall_progress = round((completed_assignments / total_assignments * 100) if total_assignments > 0 else 0)
    
    # Sort upcoming assignments by due date
    upcoming_assignments.sort(key=lambda x: x['due_date'])
    
    # Get recent activities with grades
    recent_activities = []
    for subject in enrolled_subjects:
        # Add assignment activities with grades
        for assignment in subject.get('assignments', []):
            grade = None
            feedback = None
            for submission in assignment.get('submissions', []):
                if str(submission.get('student_id')) == str(current_user.id):
                    grade = submission.get('grade')
                    feedback = submission.get('feedback')
                    break
                    
            activity = {
                'title': f"Assignment: {assignment.get('title')}",
                'description': assignment.get('description', '')[:100] + '...',
                'date': assignment.get('created_at', datetime.utcnow()),
                'subject_name': subject.get('name'),
                'grade': grade,
                'feedback': feedback
            }
            recent_activities.append(activity)
            
        # Add module activities
        for module in subject.get('modules', []):
            activity = {
                'title': f"New Module: {module.get('title')}",
                'description': module.get('description', '')[:100] + '...',
                'date': module.get('created_at', datetime.utcnow()),
                'subject_name': subject.get('name')
            }
            recent_activities.append(activity)
    
    # Sort activities by date and limit to 5
    recent_activities.sort(key=lambda x: x['date'], reverse=True)
    recent_activities = recent_activities[:5]

    # Calculate pending assignments
    pending_assignments = total_assignments - completed_assignments

    return render_template('student_dashboard.html', 
                         enrolled_subjects=enrolled_subjects,
                         recent_activities=recent_activities,
                         total_assignments=total_assignments,
                         completed_assignments=completed_assignments,
                         pending_assignments=pending_assignments,
                         upcoming_assignments=upcoming_assignments,
                         overall_progress=overall_progress,
                         current_time=current_time)

@app.route('/available_subjects')
@login_required
def available_subjects():
    try:
        if current_user.role != 'student':
            flash('Access denied. Student privileges required.', 'danger')
            return redirect(url_for('index'))
        
        # Use aggregation to get subjects with enrollment status
        pipeline = [
            {
                '$lookup': {
                    'from': 'users',
                    'localField': 'teacher_id',
                    'foreignField': '_id',
                    'as': 'teacher'
                }
            },
            {
                '$unwind': '$teacher'
            },
            {
                '$lookup': {
                    'from': 'enrollment_requests',
                    'let': {'subject_id': '$_id', 'student_id': ObjectId(current_user.id)},
                    'pipeline': [
                        {
                            '$match': {
                                '$expr': {
                                    '$and': [
                                        {'$eq': ['$subject_id', '$$subject_id']},
                                        {'$eq': ['$student_id', '$$student_id']}
                                    ]
                                }
                            }
                        }
                    ],
                    'as': 'pending_request'
                }
            },
            {
                '$addFields': {
                    'is_enrolled': {
                        '$in': [
                            ObjectId(current_user.id),
                            {
                                '$map': {
                                    'input': {'$ifNull': ['$enrollments', []]},
                                    'as': 'enrollment',
                                    'in': '$$enrollment.student_id'
                                }
                            }
                        ]
                    },
                    'has_pending_request': {'$gt': [{'$size': '$pending_request'}, 0]}
                }
            },
            {
                '$project': {
                    'name': 1,
                    'description': 1,
                    'subject_code': 1,
                    'grade_level': 1,
                    'teacher_name': '$teacher.username',
                    'teacher_id': 1,
                    'is_enrolled': 1,
                    'has_pending_request': 1,
                    'enrollments': {'$size': {'$ifNull': ['$enrollments', []]}},
                    'created_at': 1
                }
            }
        ]
        
        subjects = list(mongo.db.subjects.aggregate(pipeline))
        
        return render_template('available_subjects.html',
                             subjects=subjects)
                             
    except Exception as e:
        app.logger.error(f"Error in available_subjects: {str(e)}")
        flash('An error occurred while loading available subjects.', 'danger')
        return render_template('available_subjects.html',
                             subjects=[])

@app.route('/request_enrollment', methods=['POST'])
@login_required
def request_enrollment():
    if current_user.role != 'student':
        return redirect(url_for('index'))

    subject_id = ObjectId(request.form['subject_id'])

    # Check if already enrolled or has pending request
    existing_enrollment = mongo.db.enrollments.find_one({
        'student_id': ObjectId(current_user.id),
        'subject_id': subject_id
    })

    existing_request = mongo.db.enrollment_requests.find_one({
        'student_id': ObjectId(current_user.id),
        'subject_id': subject_id,
        'status': 'pending'
    })

    if existing_enrollment or existing_request:
        flash('You have already enrolled or requested enrollment in this subject')
        return redirect(url_for('available_subjects'))

    # Create enrollment request
    request_data = {
        'student_id': ObjectId(current_user.id),
        'subject_id': subject_id,
        'status': 'pending',
        'requested_at': datetime.utcnow()
    }
    mongo.db.enrollment_requests.insert_one(request_data)
    flash('Enrollment request sent successfully!')
    return redirect(url_for('available_subjects'))

@app.route('/personalized_learning')
@login_required
def personalized_learning():
    if current_user.role != 'student':
        return redirect(url_for('index'))
    
    learning_paths = list(mongo.db.learning_paths.find({
        'student_id': ObjectId(current_user.id)
    }))
    
    return render_template('personalized_learning.html', learning_paths=learning_paths)

@app.route('/create_custom_path', methods=['POST'])
@login_required
def create_custom_path():
    try:
        # Get form data
        subject_title = request.form['subject_title']
        current_knowledge = request.form['current_knowledge']
        time_commitment = int(request.form['time_commitment'])
        
        # Get selected resources
        resources = request.form.getlist('resources[]')
        
        # Process chapters
        chapters = []
        chapter_index = 1
        while f'chapters[{chapter_index}][title]' in request.form:
            chapter = {
                'title': request.form[f'chapters[{chapter_index}][title]'],
                'objectives': request.form[f'chapters[{chapter_index}][objectives]'],
                'progress': 0,
                'resources': {
                    'videos': [],
                    'articles': [],
                    'qa': []
                }
            }
            chapters.append(chapter)
            chapter_index += 1

        # Create learning path document
        learning_path = {
            'student_id': ObjectId(current_user.id),
            'subject_title': subject_title,
            'current_knowledge': current_knowledge,
            'time_commitment': time_commitment,
            'preferred_resources': resources,
            'modules': chapters,
            'progress': 0,
            'created_at': datetime.utcnow(),
            'last_updated': datetime.utcnow()
        }

        # Insert into database
        result = mongo.db.learning_paths.insert_one(learning_path)

        if result.inserted_id:
            flash('Learning path created successfully!', 'success')
            return redirect(url_for('student_dashboard'))
        else:
            flash('Error creating learning path. Please try again.', 'error')
            return redirect(url_for('personalized_learning'))

    except Exception as e:
        print(f"Error creating learning path: {str(e)}")
        flash('Error creating learning path. Please try again.', 'error')
        return redirect(url_for('personalized_learning'))

@app.route('/continue_learning/<learning_path_id>')
@login_required
def continue_learning(learning_path_id):
    try:
        print(f"Accessing continue_learning with path_id: {learning_path_id}")
        # Get the learning path
        learning_path = mongo.db.learning_paths.find_one({
            '_id': ObjectId(learning_path_id)
        })
        
        print(f"Learning path data: {learning_path}")
        
        if not learning_path:
            print("Learning path not found")
            flash('Learning path not found', 'error')
            return redirect(url_for('student_dashboard'))

        # Get current module
        if 'modules' not in learning_path or not learning_path['modules']:
            flash('No modules found in this learning path', 'error')
            return redirect(url_for('student_dashboard'))

        current_module = None
        for module in learning_path['modules']:
            if module.get('progress', 0) < 100:
                current_module = module
                break
        
        if not current_module:
            current_module = learning_path['modules'][0]

        # Create a more specific search query using subject and module
        subject = learning_path.get('subject_title', '').lower()
        module_title = current_module['title'].lower()
        
        # Build a targeted search query
        search_terms = []
        if 'python' in subject:
            search_terms.append('python')
        if 'data mining' in module_title or 'datamining' in module_title:
            search_terms.extend(['data mining', 'tutorial', 'introduction'])
        elif 'machine learning' in module_title:
            search_terms.extend(['machine learning', 'tutorial', 'beginners'])
        else:
            search_terms.extend([module_title, subject, 'programming', 'tutorial'])

        search_query = ' '.join(search_terms)
        print(f"Searching YouTube for: {search_query}")
        videos = search_youtube_videos(search_query)
        print(f"Found {len(videos)} videos")

        return render_template('continue_learning.html',
                             learning_path=learning_path,
                             current_module=current_module,
                             videos=videos)
                             
    except Exception as e:
        print(f"Error in continue_learning: {str(e)}")
        flash('An error occurred', 'error')
        return redirect(url_for('student_dashboard'))

@app.route('/view_learning_path/<path_id>')
@login_required
def view_learning_path(path_id):
    try:
        # Get the learning path
        learning_path = mongo.db.learning_paths.find_one({
            '_id': ObjectId(path_id),
            'student_id': ObjectId(current_user.id)
        })
        
        if not learning_path:
            flash('Learning path not found', 'error')
            return redirect(url_for('dashboard'))
            
        return render_template(
            'view_learning_path.html',
            learning_path=learning_path
        )
        
    except Exception as e:
        print(f"Error viewing learning path: {str(e)}")
        flash('Error viewing learning path', 'error')
        return redirect(url_for('dashboard'))



@app.route('/delete_learning_path/<path_id>', methods=['POST'])
@login_required
def delete_learning_path(path_id):
    try:
        # Convert string ID to ObjectId
        path_id = ObjectId(path_id)
        
        # Get the current user
        current_user_id = ObjectId(current_user.id)
        
        # Find and delete the learning path
        result = mongo.db.learning_paths.delete_one({
            '_id': path_id,
            'student_id': current_user_id  # Ensure the path belongs to the current user
        })
        
        if result.deleted_count > 0:
            # Successfully deleted
            return jsonify({'success': True, 'message': 'Learning path deleted successfully'})
        else:
            # Path not found or doesn't belong to user
            return jsonify({'success': False, 'message': 'Learning path not found'}), 404
            
    except Exception as e:
        print(f"Error deleting learning path: {str(e)}")
        return jsonify({'success': False, 'message': 'Error deleting learning path'}), 500


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/search_youtube_videos')
def search_youtube_videos_route():
    query = request.args.get('query', '')
    if not query:
        return jsonify({'error': 'No query provided'}), 400

    videos = search_youtube_videos(query)
    if videos is None:
        return jsonify({'error': 'Failed to fetch videos'}), 500

    return jsonify(videos)

@app.route('/get_module_videos')
@login_required
def get_module_videos():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject')

        if not module:
            return jsonify({'error': 'Module title is required'}), 400

        # Build a targeted search query
        search_terms = []
        if subject and 'python' in subject.lower():
            search_terms.append('python')

        module_lower = module.lower()
        if 'data mining' in module_lower or 'datamining' in module_lower:
            search_terms.extend(['data mining', 'tutorial', 'introduction'])
        elif 'machine learning' in module_lower:
            search_terms.extend(['machine learning', 'tutorial', 'beginners'])
        else:
            search_terms.extend([module, subject, 'programming', 'tutorial'])

        search_query = ' '.join(search_terms)
        print(f"Searching YouTube for: {search_query}")
        videos = search_youtube_videos(search_query)
        print(f"Found {len(videos)} videos")

        return jsonify({'videos': videos})

    except Exception as e:
        print(f"Error getting module videos: {str(e)}")
        return jsonify({'error': 'Failed to fetch videos'}), 500

@app.route('/get_module_docs')
@login_required
def get_module_docs():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject')

        print(f"Getting docs for module: {module}, subject: {subject}")  # Debug log

        if not module:
            print("No module provided")  # Debug log
            return jsonify({'error': 'Module title is required'}), 400

        # Get resources using Gemini
        resources = get_resources_from_gemini(module, subject)
        return jsonify({'docs': resources['docs']})

    except Exception as e:
        print(f"Error getting module docs: {str(e)}")  # Error log
        return jsonify({'error': 'Failed to fetch documentation'}), 500

@app.route('/get_module_articles')
@login_required
def get_module_articles():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject')

        if not module:
            return jsonify({'error': 'Module title is required'}), 400

        # Get resources using Gemini
        resources = get_resources_from_gemini(module, subject)
        return jsonify({'articles': resources['articles']})

    except Exception as e:
        print(f"Error getting module articles: {str(e)}")
        return jsonify({'error': 'Failed to fetch articles'}), 500



@app.route('/get_module_quiz')
@login_required
def get_module_quiz():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject', '')
        
        print(f"Generating quiz for module: {module}, subject: {subject}")  # Debug log
        
        if not module:
            return jsonify({'error': 'Module title is required'}), 400
            
        # Create a specific prompt for quiz generation
        prompt = f"""
        Create a quiz about {module} {f'in {subject}' if subject else ''}.
        Return exactly 3 multiple choice questions in this JSON format:
        {{
            "quiz": [
                {{
                    "id": "q1",
                    "question": "Question text here?",
                    "options": ["Option A", "Option B", "Option C", "Option D"],
                    "correct": 0,
                    "explanation": "Explanation why Option A is correct"
                }}
            ]
        }}
        Make questions relevant to {module} concepts.
        """
        
        try:
            # Initialize Gemini
            model = genai.GenerativeModel('gemini-pro')
            response = model.generate_content(prompt)
            
            print(f"Gemini response: {response.text}")  # Debug log
            
            # Clean up response text
            response_text = response.text.strip()
            if response_text.startswith('```json'):
                response_text = response_text[7:]
            if response_text.endswith('```'):
                response_text = response_text[:-3]
            response_text = response_text.strip()
            
            # Parse JSON
            data = json.loads(response_text)
            
            if 'quiz' not in data or not data['quiz']:
                raise ValueError("No quiz data in response")
                
            return jsonify(data)
            
        except Exception as e:
            print(f"Error generating quiz from Gemini: {str(e)}")
            # Return fallback quiz
            fallback_quiz = {
                'quiz': [
                    {
                        'id': 'q1',
                        'question': f'What is the main purpose of studying {module}?',
                        'options': [
                            f'To understand {module} fundamentals',
                            f'To apply {module} in practice',
                            f'To solve problems using {module}',
                            'All of the above'
                        ],
                        'correct': 3,
                        'explanation': f'Studying {module} involves understanding fundamentals, practical application, and problem-solving.'
                    },
                    {
                        'id': 'q2',
                        'question': f'Which approach is most effective when learning {module}?',
                        'options': [
                            'Theory only',
                            'Practice only',
                            'Both theory and practice',
                            'Neither theory nor practice'
                        ],
                        'correct': 2,
                        'explanation': 'A balanced approach of both theory and practice leads to the best learning outcomes.'
                    },
                    {
                        'id': 'q3',
                        'question': f'What is a key benefit of mastering {module}?',
                        'options': [
                            'Enhanced problem-solving skills',
                            'Better understanding of concepts',
                            'Practical application abilities',
                            'All of the above'
                        ],
                        'correct': 3,
                        'explanation': f'Mastering {module} provides multiple benefits including problem-solving, conceptual understanding, and practical skills.'
                    }
                ]
            }
            return jsonify(fallback_quiz)
            
    except Exception as e:
        print(f"Error in quiz route: {str(e)}")
        return jsonify({'error': 'Failed to generate quiz'}), 500


def get_resources_from_gemini(module_name, subject):
    """Use Gemini to find relevant learning resources for any subject"""
    try:
        # Initialize Gemini
        genai.configure(api_key=app.config['GEMINI_API_KEY'])
        model = genai.GenerativeModel('gemini-pro')
        
        # Create prompt for documentation
        docs_prompt = f"""
        Find 2-3 high-quality documentation or reference resources for learning about "{module_name}" in the subject area of "{subject}".
        Focus on official documentation, educational websites, and reliable reference materials.
        Return ONLY the response in this EXACT JSON format:
        {{
            "docs": [
                {{
                    "id": "unique-id",
                    "title": "Resource Title",
                    "description": "Brief description (max 100 chars)",
                    "url": "Direct URL to resource"
                }}
            ]
        }}
        Rules:
        1. URLs must be direct links to actual documentation pages
        2. Focus on official documentation when available
        3. Include educational websites like coursera, edx, or university resources
        4. Verify the URLs exist and are accessible
        5. No placeholder or example URLs
        """
        
        # Create prompt for articles
        articles_prompt = f"""
        Find 2-3 high-quality tutorial articles or guides for learning about "{module_name}" in the subject area of "{subject}".
        Focus on well-written tutorials, practical guides, and educational blog posts.
        Return ONLY the response in this EXACT JSON format:
        {{
            "articles": [
                {{
                    "id": "unique-id",
                    "title": "Article Title",
                    "description": "Brief description (max 100 chars)",
                    "url": "Direct URL to article"
                }}
            ]
        }}
        Rules:
        1. URLs must be direct links to actual articles
        2. Focus on reputable educational websites and blogs
        3. Include tutorials from platforms like Medium, Dev.to, or educational blogs
        4. Verify the URLs exist and are accessible
        5. No placeholder or example URLs
        """
        
        # Get responses from Gemini
        docs_response = model.generate_content(docs_prompt)
        articles_response = model.generate_content(articles_prompt)
        
        try:
            # Parse JSON responses
            docs_data = json.loads(docs_response.text)
            articles_data = json.loads(articles_response.text)
            
            # Validate URLs in responses
            for doc in docs_data.get('docs', []):
                if not doc.get('url', '').startswith('http'):
                    raise ValueError(f"Invalid URL in documentation: {doc.get('url')}")
                    
            for article in articles_data.get('articles', []):
                if not article.get('url', '').startswith('http'):
                    raise ValueError(f"Invalid URL in article: {article.get('url')}")
            
            return {
                'docs': docs_data.get('docs', []),
                'articles': articles_data.get('articles', [])
            }
            
        except json.JSONDecodeError:
            print(f"Error parsing Gemini response - Docs: {docs_response.text}")
            print(f"Error parsing Gemini response - Articles: {articles_response.text}")
            raise
        
    except Exception as e:
        print(f"Error getting resources from Gemini: {str(e)}")
        # Fallback to web search if Gemini fails
        return get_fallback_resources(module_name, subject)

def get_fallback_resources(module_name, subject):
    """Fallback function to get basic resources if Gemini fails"""
    try:
        # Create search queries
        search_terms = f"{subject} {module_name}"
        encoded_terms = urllib.parse.quote(search_terms)
        
        # Generate educational website URLs
        resources = {
            'docs': [
                {
                    'id': 'coursera',
                    'title': f'Coursera: {module_name}',
                    'description': f'Online courses about {module_name}',
                    'url': f'https://www.coursera.org/search?query={encoded_terms}'
                },
                {
                    'id': 'edx',
                    'title': f'edX: {module_name}',
                    'description': f'Free online courses about {module_name}',
                    'url': f'https://www.edx.org/search?q={encoded_terms}'
                }
            ],
            'articles': [
                {
                    'id': 'medium',
                    'title': f'Medium: {module_name}',
                    'description': f'Articles and tutorials about {module_name}',
                    'url': f'https://medium.com/search?q={encoded_terms}'
                },
                {
                    'id': 'dev-to',
                    'title': f'Dev.to: {module_name}',
                    'description': f'Developer tutorials about {module_name}',
                    'url': f'https://dev.to/search?q={encoded_terms}'
                }
            ]
        }
        
        return resources
        
    except Exception as e:
        print(f"Error in fallback resources: {str(e)}")
        return {'docs': [], 'articles': []}


UPLOAD_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static', 'uploads'))
print(f"Debug: Upload folder path: {UPLOAD_FOLDER}")

# Ensure the uploads directory exists
try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    print(f"Debug: Created upload folder at {UPLOAD_FOLDER}")
except Exception as e:
    print(f"Error creating upload folder: {str(e)}")

ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'txt', 'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_uploaded_file(file, subject_id):
    """Helper function to save an uploaded file to GridFS"""
    try:
        if not file or not file.filename:
            print("Debug: No file or filename provided")
            return None
            
        original_filename = file.filename
        if not allowed_file(original_filename):
            print(f"Debug: File type not allowed for {original_filename}")
            return None
            
        # Generate a secure filename with timestamp
        secure_name = secure_filename(original_filename)
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{secure_name}"
        
        print(f"Debug: Processing file: {filename}")
        
        try:
            # Store file in GridFS
            file_id = fs.put(
                file.stream,
                filename=filename,
                content_type=file.content_type,
                subject_id=str(subject_id),
                original_filename=original_filename
            )
            
            print(f"Debug: File saved to GridFS with ID: {file_id}")
            
            # Get file size
            file_size = fs.get(file_id).length
            print(f"Debug: File size: {file_size} bytes")
            
            return {
                '_id': ObjectId(),
                'original_filename': original_filename,
                'filename': filename,
                'file_id': file_id,  # Store GridFS file ID
                'file_size': file_size,
                'file_type': original_filename.rsplit('.', 1)[1].lower(),
                'uploaded_at': datetime.utcnow()
            }
            
        except Exception as e:
            print(f"Error saving file to GridFS: {str(e)}")
            return None
            
    except Exception as e:
        print(f"Error in save_uploaded_file: {str(e)}")
        return None

@app.route('/teacher_dashboard')
@login_required
def teacher_dashboard():
    try:
        if current_user.role != 'teacher':
            flash('Access denied. Teacher privileges required.', 'danger')
            return redirect(url_for('index'))
        
        # Use aggregation pipeline to get subjects with enrollment counts
        pipeline = [
            {
                '$match': {
                    'teacher_id': ObjectId(current_user.id)
                }
            },
            {
                '$lookup': {
                    'from': 'enrollment_requests',
                    'let': {'subject_id': '$_id'},
                    'pipeline': [
                        {
                            '$match': {
                                '$expr': {'$eq': ['$subject_id', '$$subject_id']},
                                'status': 'pending'  # Only get pending requests
                            }
                        }
                    ],
                    'as': 'pending_requests'
                }
            },
            {
                '$lookup': {
                    'from': 'users',
                    'let': {'enrollments': '$enrollments'},
                    'pipeline': [
                        {
                            '$match': {
                                '$expr': {
                                    '$in': ['$_id', {
                                        '$map': {
                                            'input': {'$ifNull': ['$$enrollments', []]},
                                            'as': 'enrollment',
                                            'in': '$$enrollment.student_id'
                                        }
                                    }]
                                }
                            }
                        }
                    ],
                    'as': 'enrolled_students'
                }
            }
        ]

        subjects = list(mongo.db.subjects.aggregate(pipeline))

        # Get all pending enrollment requests for display
        enrollment_requests = list(mongo.db.enrollment_requests.aggregate([
            {
                '$match': {
                    'status': 'pending'  # Only get pending requests
                }
            },
            {
                '$lookup': {
                    'from': 'subjects',
                    'localField': 'subject_id',
                    'foreignField': '_id',
                    'as': 'subject'
                }
            },
            {
                '$unwind': '$subject'
            },
            {
                '$match': {
                    'subject.teacher_id': ObjectId(current_user.id)
                }
            },
            {
                '$lookup': {
                    'from': 'users',
                    'localField': 'student_id',
                    'foreignField': '_id',
                    'as': 'student'
                }
            },
            {
                '$unwind': '$student'
            }
        ]))

        # Format enrollment requests for template
        formatted_requests = []
        for req in enrollment_requests:
            formatted_requests.append({
                '_id': req['_id'],
                'student_name': f"{req['student'].get('first_name', '')} {req['student'].get('last_name', '')}",
                'student_email': req['student'].get('email', 'No email'),
                'subject_name': req['subject'].get('name', 'Unknown Subject'),
                'created_at': req.get('created_at', datetime.utcnow())
            })

        return render_template('teacher_dashboard.html', 
                             subjects=subjects,
                             enrollment_requests=formatted_requests)

    except Exception as e:
        print(f"Debug: Error in teacher_dashboard: {str(e)}")
        flash('An error occurred while loading the dashboard.', 'danger')
        return redirect(url_for('index'))

@app.route('/new_subject', methods=['GET', 'POST'])
@login_required
def new_subject():
    if current_user.role != 'teacher':
        flash('Access denied. Teacher privileges required.', 'danger')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        subject_data = {
            'name': request.form.get('name'),
            'description': request.form.get('description'),
            'teacher_id': ObjectId(current_user.id),
            'modules': [],
            'created_at': datetime.utcnow()
        }
        mongo.db.subjects.insert_one(subject_data)
        flash('Subject created successfully!', 'success')
        return redirect(url_for('teacher_dashboard'))
    
    return render_template('new_subject.html')

@app.route('/manage_subject/<subject_id>')
@login_required
def manage_subject(subject_id):
    if current_user.role != 'teacher':
        flash('Access denied. Teacher privileges required.', 'danger')
        return redirect(url_for('index'))
    
    subject = mongo.db.subjects.find_one({'_id': ObjectId(subject_id)})
    if not subject:
        flash('Subject not found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    # Get all enrolled students
    students = []
    for enrollment in subject.get('enrollments', []):
        student = mongo.db.users.find_one({'_id': ObjectId(enrollment['student_id'])})
        if student:
            # Add enrollment info to student object
            student['enrolled_at'] = enrollment.get('enrolled_at')
            student['enrollment_status'] = enrollment.get('status', 'pending')
            # Calculate progress (you can modify this based on your needs)
            completed_assignments = 0
            total_assignments = len(subject.get('assignments', []))
            progress = (completed_assignments / total_assignments * 100) if total_assignments > 0 else 0
            student['progress'] = round(progress)
            students.append(student)

    return render_template('manage_subject.html', 
                         subject=subject, 
                         students=students,
                         current_time=datetime.utcnow())

@app.route('/handle_enrollment/<request_id>/<action>')
@login_required
def handle_enrollment(request_id, action):
    try:
        if current_user.role != 'teacher':
            flash('Access denied. Teacher privileges required.', 'danger')
            return redirect(url_for('index'))

        # Get the enrollment request
        enrollment_request = mongo.db.enrollment_requests.find_one({'_id': ObjectId(request_id)})
        if not enrollment_request:
            flash('Enrollment request not found.', 'danger')
            return redirect(url_for('teacher_dashboard'))

        # Check if request was already handled
        if enrollment_request.get('status') in ['approved', 'rejected']:
            flash(f'This enrollment request was already {enrollment_request["status"]}.', 'warning')
            return redirect(url_for('teacher_dashboard'))

        # Get the subject
        subject = mongo.db.subjects.find_one({'_id': enrollment_request['subject_id']})
        if not subject:
            flash('Subject not found.', 'danger')
            return redirect(url_for('teacher_dashboard'))

        if action == 'approve':
            # Check if student is already enrolled
            if any(enroll.get('student_id') == enrollment_request['student_id'] 
                  for enroll in subject.get('enrollments', [])):
                flash('Student is already enrolled in this subject.', 'warning')
                return redirect(url_for('teacher_dashboard'))

            # Add student to subject's enrollment list with enrollment date
            mongo.db.subjects.update_one(
                {'_id': subject['_id']},
                {'$push': {'enrollments': {
                    'student_id': enrollment_request['student_id'],
                    'enrolled_at': datetime.utcnow()
                }}}
            )

            # Update request status
            mongo.db.enrollment_requests.update_one(
                {'_id': ObjectId(request_id)},
                {'$set': {'status': 'approved'}}
            )

            # Add notification for the student
            mongo.db.notifications.insert_one({
                'user_id': enrollment_request['student_id'],
                'type': 'enrollment_approved',
                'message': f'Your enrollment in {subject["name"]} has been approved!',
                'timestamp': datetime.utcnow(),
                'read': False
            })

            flash('Enrollment request approved successfully.', 'success')

        elif action == 'reject':
            # Update request status
            mongo.db.enrollment_requests.update_one(
                {'_id': ObjectId(request_id)},
                {'$set': {'status': 'rejected'}}
            )

            # Add notification for the student
            mongo.db.notifications.insert_one({
                'user_id': enrollment_request['student_id'],
                'type': 'enrollment_rejected',
                'message': f'Your enrollment request for {subject["name"]} has been rejected.',
                'timestamp': datetime.utcnow(),
                'read': False
            })

            flash('Enrollment request rejected.', 'info')

        return redirect(url_for('teacher_dashboard'))

    except Exception as e:
        print(f"Debug: Error in handle_enrollment: {str(e)}")
        flash('An error occurred while processing the enrollment request.', 'danger')
        return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/analytics/<subject_id>')
@login_required
def subject_analytics(subject_id):
    if current_user.role != 'teacher':
        flash('Access denied')
        return redirect(url_for('index'))
    
    subject = mongo.db.subjects.find_one({'_id': ObjectId(subject_id)})
    if not subject:
        flash('Subject not found')
        return redirect(url_for('teacher_dashboard'))

    # Collect analytics data
    student_progress = defaultdict(dict)
    quiz_results = mongo.db.quiz_results.find({'subject_id': subject_id})
    module_completion = mongo.db.module_progress.find({'subject_id': subject_id})
    
    analytics = {
        'completion_rate': calculate_completion_rate(module_completion),
        'quiz_performance': analyze_quiz_performance(quiz_results),
        'active_students': get_active_students(subject_id),
        'engagement_metrics': calculate_engagement_metrics(subject_id)
    }
    
    return jsonify(analytics)

def calculate_completion_rate(module_progress):
    total_modules = len(set(p['module_id'] for p in module_progress))
    completed = len(set(p['module_id'] for p in module_progress if p.get('completed')))
    return (completed / total_modules * 100) if total_modules > 0 else 0

def analyze_quiz_performance(quiz_results):
    performance = defaultdict(list)
    for result in quiz_results:
        performance['scores'].append(result.get('score', 0))
        performance['attempts'].append(result.get('attempts', 0))
    
    return {
        'average_score': sum(performance['scores']) / len(performance['scores']) if performance['scores'] else 0,
        'total_attempts': sum(performance['attempts'])
    }

def get_active_students(subject_id):
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    active_count = mongo.db.student_activity.count_documents({
        'subject_id': subject_id,
        'last_active': {'$gte': week_ago}
    })
    return active_count

def calculate_engagement_metrics(subject_id):
    activities = mongo.db.student_activity.find({
        'subject_id': subject_id
    }).sort('timestamp', -1).limit(100)
    
    metrics = {
        'video_views': 0,
        'quiz_attempts': 0,
        'resource_downloads': 0,
        'discussion_posts': 0
    }
    
    for activity in activities:
        activity_type = activity.get('type')
        metrics[activity_type] = metrics.get(activity_type, 0) + 1
    
    return metrics

@app.route('/student/progress')
@login_required
def student_progress():
    if current_user.role != 'student':
        flash('Access denied. Student privileges required.', 'danger')
        return redirect(url_for('index'))

    # Get enrolled subjects
    enrolled_subjects = list(mongo.db.subjects.find({
        'enrollments.student_id': ObjectId(current_user.id)
    }))

    # Calculate progress for each subject
    for subject in enrolled_subjects:
        completed_modules = get_completed_modules(current_user.id, subject['_id'])
        total_modules = len(subject.get('modules', []))
        subject['progress'] = round((len(completed_modules) / total_modules * 100) if total_modules > 0 else 0)
        
        # Get achievements
        subject['achievements'] = get_achievements(current_user.id, subject['_id'])
        
        # Get recent activity
        subject['recent_activity'] = get_recent_activity(current_user.id, subject['_id'])

    return render_template('student_progress.html', subjects=enrolled_subjects)

def get_completed_modules(student_id, subject_id):
    completed = mongo.db.module_progress.find({
        'student_id': student_id,
        'subject_id': subject_id,
        'completed': True
    })
    return list(completed)

def get_achievements(student_id, subject_id):
    achievements = []
    
    # Check for various achievements
    quiz_scores = mongo.db.quiz_results.find({
        'student_id': student_id,
        'subject_id': subject_id
    })
    
    # Perfect Score Achievement
    perfect_scores = sum(1 for score in quiz_scores if score.get('score') == 100)
    if perfect_scores >= 1:
        achievements.append({
            'name': 'Perfect Score',
            'description': 'Achieved 100% on a quiz',
            'icon': '🏆'
        })
    
    # Quick Learner Achievement
    module_completions = mongo.db.module_progress.find({
        'student_id': student_id,
        'subject_id': subject_id,
        'completed': True
    })
    
    if len(list(module_completions)) >= 5:
        achievements.append({
            'name': 'Quick Learner',
            'description': 'Completed 5 modules',
            'icon': '📚'
        })
    
    return achievements

def get_recent_activity(student_id, subject_id):
    activities = mongo.db.student_activity.find({
        'student_id': student_id,
        'subject_id': subject_id
    }).sort('timestamp', -1).limit(10)
    
    return list(activities)

@app.route('/student/track-activity', methods=['POST'])
@login_required
def track_activity():
    if current_user.role != 'student':
        return jsonify({'error': 'Access denied'}), 403
    
    activity_data = request.json
    activity_data.update({
        'student_id': current_user.id,
        'timestamp': datetime.utcnow()
    })
    
    mongo.db.student_activity.insert_one(activity_data)
    check_and_award_achievements(current_user.id, activity_data['subject_id'])
    
    return jsonify({'status': 'success'})

def check_and_award_achievements(student_id, subject_id):
    # Check for new achievements based on recent activity
    new_achievements = []
    
    # Example: Check for "Consistent Learner" achievement
    recent_days = mongo.db.student_activity.distinct('timestamp', {
        'student_id': student_id,
        'subject_id': subject_id,
        'timestamp': {'$gte': datetime.utcnow() - timedelta(days=7)}
    })
    
    if len(recent_days) >= 5:  # Active for 5 different days in a week
        new_achievements.append({
            'name': 'Consistent Learner',
            'description': 'Studied for 5 different days in a week',
            'icon': '🌟'
        })
    
    # Store new achievements
    if new_achievements:
        mongo.db.achievements.insert_many([{
            'student_id': student_id,
            'subject_id': subject_id,
            'achievement': achievement,
            'earned_at': datetime.utcnow()
        } for achievement in new_achievements])
        
        # Notify student of new achievements
        for achievement in new_achievements:
            mongo.db.notifications.insert_one({
                'user_id': student_id,
                'type': 'achievement',
                'message': f'Congratulations! You earned the {achievement["name"]} achievement!',
                'timestamp': datetime.utcnow(),
                'read': False
            })

@app.route('/content/recommend/<subject_id>')
@login_required
def get_content_recommendations(subject_id):
    # Get user's learning history
    user_history = mongo.db.student_activity.find({
        'student_id': current_user.id,
        'subject_id': subject_id
    }).sort('timestamp', -1)
    
    # Analyze user's performance and preferences
    content_preferences = analyze_user_preferences(user_history)
    
    # Get personalized recommendations
    recommendations = generate_recommendations(subject_id, content_preferences)
    
    return jsonify(recommendations)

def analyze_user_preferences(user_history):
    preferences = {
        'preferred_content_type': None,
        'difficulty_level': 'intermediate',
        'learning_pace': 'normal',
        'successful_topics': [],
        'struggling_topics': []
    }
    
    content_type_counts = defaultdict(int)
    topic_performance = defaultdict(list)
    
    for activity in user_history:
        # Track content type preferences
        if 'content_type' in activity:
            content_type_counts[activity['content_type']] += 1
        
        # Track topic performance
        if 'quiz_result' in activity:
            topic_performance[activity['topic']].append(activity['quiz_result'])
    
    # Determine preferred content type
    if content_type_counts:
        preferences['preferred_content_type'] = max(content_type_counts.items(), key=lambda x: x[1])[0]
    
    # Analyze topic performance
    for topic, scores in topic_performance.items():
        avg_score = sum(scores) / len(scores)
        if avg_score >= 80:
            preferences['successful_topics'].append(topic)
        elif avg_score <= 50:
            preferences['struggling_topics'].append(topic)
    
    return preferences

def generate_recommendations(subject_id, preferences):
    recommendations = []
    
    # Get all available content for the subject
    subject_content = mongo.db.content.find({
        'subject_id': subject_id
    })
    
    for content in subject_content:
        relevance_score = calculate_content_relevance(content, preferences)
        if relevance_score > 0.5:  # Threshold for recommendation
            recommendations.append({
                'content_id': str(content['_id']),
                'title': content['title'],
                'type': content['type'],
                'description': content['description'],
                'relevance_score': relevance_score,
                'estimated_time': content.get('estimated_time', '15 mins'),
                'difficulty': content.get('difficulty', 'intermediate')
            })
    
    # Sort by relevance score
    recommendations.sort(key=lambda x: x['relevance_score'], reverse=True)
    return recommendations[:10]  # Return top 10 recommendations

def calculate_content_relevance(content, preferences):
    relevance_score = 0.0
    
    # Content type match
    if content['type'] == preferences['preferred_content_type']:
        relevance_score += 0.3
    
    # Difficulty level match
    if content.get('difficulty') == preferences['difficulty_level']:
        relevance_score += 0.2
    
    # Topic relevance
    content_topic = content.get('topic')
    if content_topic in preferences['struggling_topics']:
        relevance_score += 0.4  # Prioritize content for struggling topics
    elif content_topic in preferences['successful_topics']:
        relevance_score += 0.1  # Still show but with lower priority
    
    return relevance_score

@app.route('/content/upload', methods=['POST'])
@login_required
def upload_content():
    if current_user.role != 'teacher':
        return jsonify({'error': 'Access denied'}), 403
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    allowed_extensions = {'pdf', 'doc', 'docx', 'ppt', 'pptx', 'mp4', 'mp3'}
    if not file.filename.split('.')[-1].lower() in allowed_extensions:
        return jsonify({'error': 'File type not supported'}), 400
    
    # Save file and create content record
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)
    
    content_data = {
        'title': request.form.get('title'),
        'description': request.form.get('description'),
        'subject_id': request.form.get('subject_id'),
        'type': request.form.get('content_type'),
        'file_path': file_path,
        'uploaded_by': current_user.id,
        'upload_date': datetime.utcnow(),
        'metadata': {
            'file_type': file.filename.split('.')[-1].lower(),
            'size': os.path.getsize(file_path),
            'duration': request.form.get('duration'),
            'difficulty': request.form.get('difficulty', 'intermediate')
        }
    }
    
    mongo.db.content.insert_one(content_data)
    return jsonify({'status': 'success', 'message': 'Content uploaded successfully'})

@app.route('/create_module/<subject_id>', methods=['POST'])
@login_required
def create_module(subject_id):
    if current_user.role != 'teacher':
        flash('Access denied. Teacher privileges required.', 'danger')
        return redirect(url_for('index'))
    
    try:
        # Get the subject
        subject = mongo.db.subjects.find_one({'_id': ObjectId(subject_id)})
        if not subject:
            flash('Subject not found.', 'danger')
            return redirect(url_for('teacher_dashboard'))

        # Create new module
        new_module = {
            'title': request.form.get('title'),
            'description': request.form.get('description'),
            'duration': int(request.form.get('duration')),
            'order': int(request.form.get('order')),
            'created_at': datetime.utcnow(),
            'created_by': current_user.id,
            'resources': [],
            'status': 'active'
        }
        
        # Update subject with new module
        result = mongo.db.subjects.update_one(
            {'_id': ObjectId(subject_id)},
            {
                '$push': {
                    'modules': new_module
                }
            }
        )
        
        if result.modified_count > 0:
            flash('Module created successfully!', 'success')
        else:
            flash('Failed to create module. Please try again.', 'danger')
            
        return redirect(url_for('manage_subject', subject_id=subject_id))
        
    except Exception as e:
        flash('An error occurred while creating the module.', 'danger')
        return redirect(url_for('manage_subject', subject_id=subject_id))

@app.route('/create_assignment/<subject_id>', methods=['POST'])
@login_required
def create_assignment(subject_id):
    if current_user.role != 'teacher':
        flash('Access denied. Teacher privileges required.', 'danger')
        return redirect(url_for('index'))
    
    try:
        print(f"Debug: Creating assignment for subject {subject_id}")
        
        # Get the subject
        try:
            subject = mongo.db.subjects.find_one({'_id': ObjectId(subject_id)})
            print(f"Debug: Subject found: {subject is not None}")
            if not subject:
                flash('Subject not found.', 'danger')
                return redirect(url_for('teacher_dashboard'))
        except Exception as e:
            print(f"Debug: Error finding subject: {str(e)}")
            flash('Invalid subject ID.', 'danger')
            return redirect(url_for('teacher_dashboard'))

        # Get form data with validation
        title = request.form.get('title')
        description = request.form.get('description')
        points = request.form.get('points')
        due_date_str = request.form.get('due_date')
        
        print(f"Debug: Form data - title: {title}, points: {points}, due_date: {due_date_str}")
        
        if not all([title, description, points, due_date_str]):
            flash('Please fill in all required fields.', 'danger')
            return redirect(url_for('manage_subject', subject_id=subject_id))

        # Parse points
        try:
            points = int(points)
            if points < 0:
                raise ValueError("Points must be positive")
        except ValueError:
            flash('Points must be a positive number.', 'danger')
            return redirect(url_for('manage_subject', subject_id=subject_id))

        # Parse the due date
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M')
            if due_date < datetime.utcnow():
                flash('Due date must be in the future.', 'danger')
                return redirect(url_for('manage_subject', subject_id=subject_id))
        except ValueError:
            flash('Invalid due date format.', 'danger')
            return redirect(url_for('manage_subject', subject_id=subject_id))
        
        # Handle file uploads
        uploaded_files = []
        if 'files' in request.files:
            files = request.files.getlist('files')
            print(f"Debug: Number of files uploaded: {len(files)}")
            
            for file in files:
                file_info = save_uploaded_file(file, subject_id)
                if file_info:
                    uploaded_files.append(file_info)
                else:
                    flash(f'Error uploading file: {file.filename}', 'danger')
        
        # Create new assignment
        new_assignment = {
            '_id': ObjectId(),  # Generate new ObjectId for assignment
            'title': title,
            'description': description,
            'points': points,
            'due_date': due_date,
            'created_at': datetime.utcnow(),
            'created_by': ObjectId(current_user.id),
            'module_id': ObjectId(request.form.get('module_id')) if request.form.get('module_id') else None,
            'files': uploaded_files,
            'submissions': [],
            'status': 'active'
        }
        
        print("Debug: Assignment object created")
        
        # Update subject with new assignment
        try:
            result = mongo.db.subjects.update_one(
                {'_id': ObjectId(subject_id)},
                {
                    '$push': {
                        'assignments': new_assignment
                    }
                }
            )
            
            print(f"Debug: Database update result - modified_count: {result.modified_count}")
            
            if result.modified_count > 0:
                flash('Assignment created successfully!', 'success')
            else:
                flash('Failed to create assignment. Please try again.', 'danger')
                # Clean up uploaded files if assignment creation failed
                for file_info in uploaded_files:
                    try:
                        if os.path.exists(file_info['absolute_path']):
                            os.remove(file_info['absolute_path'])
                    except Exception as e:
                        print(f"Error cleaning up file: {str(e)}")
                        
        except Exception as e:
            print(f"Debug: Error updating database: {str(e)}")
            # Clean up uploaded files if assignment creation failed
            for file_info in uploaded_files:
                try:
                    if os.path.exists(file_info['absolute_path']):
                        os.remove(file_info['absolute_path'])
                except Exception as e:
                    print(f"Error cleaning up file: {str(e)}")
            flash('Database error while creating assignment.', 'danger')
            
        return redirect(url_for('manage_subject', subject_id=subject_id))
        
    except Exception as e:
        print(f"Debug: Unhandled error in create_assignment: {str(e)}")
        flash('An error occurred while creating the assignment.', 'danger')
        return redirect(url_for('manage_subject', subject_id=subject_id))

@app.route('/enroll_subject', methods=['POST'])
@login_required
def enroll_subject():
    if current_user.role != 'student':
        flash('Access denied. Student privileges required.', 'danger')
        return redirect(url_for('index'))
    
    subject_code = request.form.get('subject_code')
    if not subject_code:
        flash('Please provide a subject code.', 'danger')
        return redirect(url_for('student_dashboard'))
    
    # Find the subject by code
    subject = mongo.db.subjects.find_one({'subject_code': subject_code})
    if not subject:
        flash('Invalid subject code. Please check and try again.', 'danger')
        return redirect(url_for('student_dashboard'))
    
    # Check if already enrolled
    existing_enrollment = mongo.db.subjects.find_one({
        '_id': subject['_id'],
        'enrollments.student_id': ObjectId(current_user.id)
    })

    if existing_enrollment:
        flash('You are already enrolled in this subject.', 'warning')
        return redirect(url_for('student_dashboard'))

    # Add enrollment request
    enrollment = {
        'student_id': ObjectId(current_user.id),
        'enrolled_at': datetime.utcnow(),
        'status': 'pending',
        'request_date': datetime.utcnow()
    }
    
    try:
        # Update subject with new enrollment
        result = mongo.db.subjects.update_one(
            {'_id': subject['_id']},
            {
                '$push': {
                    'enrollments': enrollment
                }
            }
        )
        
        if result.modified_count > 0:
            flash('Enrollment request submitted successfully! Waiting for teacher approval.', 'success')
        else:
            flash('Failed to submit enrollment request. Please try again.', 'danger')
            
    except Exception as e:
        flash('An error occurred while processing your enrollment request.', 'danger')
    
    return redirect(url_for('student_dashboard'))

@app.route('/view_subject/<subject_id>')
@login_required
def view_subject(subject_id):
    try:
        # Convert subject_id to ObjectId
        try:
            subject_id_obj = ObjectId(subject_id)
        except Exception as e:
            print(f"Debug: Invalid subject_id format: {str(e)}")
            flash('Invalid subject ID format.', 'danger')
            return redirect(url_for('student_dashboard'))

        # Find the subject
        subject = mongo.db.subjects.find_one({'_id': subject_id_obj})
        if not subject:
            flash('Subject not found.', 'danger')
            return redirect(url_for('student_dashboard'))

        # Get teacher details
        teacher_id = subject.get('teacher_id')
        if teacher_id:
            teacher = mongo.db.users.find_one({'_id': ObjectId(teacher_id)})
            if teacher:
                subject['teacher_name'] = f"{teacher.get('first_name', '')} {teacher.get('last_name', '')}"
                subject['teacher_email'] = teacher.get('email', '')
            else:
                subject['teacher_name'] = 'Unknown Teacher'
                subject['teacher_email'] = ''
        else:
            subject['teacher_name'] = 'Unknown Teacher'
            subject['teacher_email'] = ''

        # Initialize assignments if not present
        if 'assignments' not in subject:
            subject['assignments'] = []

        # Calculate progress
        total_assignments = len(subject['assignments'])
        completed_assignments = 0
        upcoming_assignments = []
        current_time = datetime.utcnow()

        for assignment in subject['assignments']:
            # Check submissions
            submissions = assignment.get('submissions', [])
            if any(str(s.get('student_id', '')) == str(current_user.id) for s in submissions):
                completed_assignments += 1

            # Check due date
            try:
                due_date = assignment.get('due_date')
                if due_date and isinstance(due_date, datetime) and due_date > current_time:
                    upcoming_assignments.append(assignment)
            except Exception as e:
                print(f"Debug: Error processing assignment due date: {str(e)}")

        progress = round((completed_assignments / total_assignments * 100) if total_assignments > 0 else 0)

        # Sort upcoming assignments
        upcoming_assignments.sort(key=lambda x: x.get('due_date', current_time))

        # Add current time to context
        subject['current_time'] = current_time

        # Initialize modules if not present
        if 'modules' not in subject:
            subject['modules'] = []

        return render_template('view_subject.html',
                             subject=subject,
                             progress=progress,
                             upcoming_assignments=upcoming_assignments[:3],
                             current_time=current_time)

    except Exception as e:
        print(f"Debug: Unhandled error in view_subject: {str(e)}")
        flash('An error occurred while loading the subject.', 'danger')
        return redirect(url_for('student_dashboard' if current_user.role == 'student' else 'teacher_dashboard'))

@app.route('/download_resource/<resource_id>')
@login_required
def download_resource(resource_id):
    if current_user.role != 'student':
        flash('Access denied. Student privileges required.', 'danger')
        return redirect(url_for('index'))
    
    try:
        # Find the resource in modules
        subject = mongo.db.subjects.find_one({
            'modules.resources._id': ObjectId(resource_id)
        })
        
        if not subject:
            flash('Resource not found.', 'danger')
            return redirect(url_for('student_dashboard'))

        # Find the resource in the modules
        resource = None
        for module in subject.get('modules', []):
            for res in module.get('resources', []):
                if res.get('_id') == ObjectId(resource_id):
                    resource = res
                    break
            if resource:
                break
        
        if not resource:
            flash('Resource not found.', 'danger')
            return redirect(url_for('view_subject', subject_id=subject['_id']))
        
        # Return the file
        return send_file(
            resource['file_path'],
            as_attachment=True,
            download_name=resource['title']
        )
        
    except Exception as e:
        flash('An error occurred while downloading the resource.', 'danger')
        return redirect(url_for('student_dashboard'))

@app.route('/submit_assignment/<assignment_id>', methods=['GET', 'POST'])
@login_required
def submit_assignment(assignment_id):
    if current_user.role != 'student':
        flash('Access denied. Student privileges required.', 'danger')
        return redirect(url_for('index'))

    try:
        # Convert assignment_id to ObjectId
        assignment_id_obj = ObjectId(assignment_id)
        
        # Find the subject containing this assignment
        subject = mongo.db.subjects.find_one({
            'assignments._id': assignment_id_obj
        })
        
        if not subject:
            flash('Assignment not found.', 'danger')
            return redirect(url_for('student_dashboard'))
            
        # Find the specific assignment
        assignment = None
        for a in subject['assignments']:
            if a.get('_id') == assignment_id_obj:
                assignment = a
                break
                
        if not assignment:
            flash('Assignment not found.', 'danger')
            return redirect(url_for('student_dashboard'))

        if request.method == 'POST':
            # Check if files were uploaded
            if 'files[]' not in request.files:
                flash('No files were uploaded.', 'danger')
                return redirect(request.url)
                
            files = request.files.getlist('files[]')
            if not files or all(not f.filename for f in files):
                flash('No files were selected.', 'danger')
                return redirect(request.url)
                
            # Save uploaded files
            saved_files = []
            for file in files:
                if file and file.filename:
                    file_info = save_uploaded_file(file, str(subject['_id']))
                    if file_info:
                        saved_files.append(file_info)
                    else:
                        flash(f'Error uploading file: {file.filename}', 'danger')
                        
            if not saved_files:
                flash('No files were successfully uploaded.', 'danger')
                return redirect(request.url)
                
            # Create submission document
            submission = {
                '_id': ObjectId(),  # Generate new ObjectId for submission
                'student_id': current_user.id,
                'files': saved_files,
                'submitted_at': datetime.utcnow()
            }
            
            # Update assignment with new submission
            result = mongo.db.subjects.update_one(
                {
                    '_id': subject['_id'],
                    'assignments._id': assignment_id_obj
                },
                {
                    '$push': {
                        'assignments.$.submissions': submission
                    }
                }
            )
            
            if result.modified_count > 0:
                flash('Assignment submitted successfully!', 'success')
                return redirect(url_for('view_assignment', assignment_id=assignment_id))
            else:
                flash('Error submitting assignment. Please try again.', 'danger')
                return redirect(request.url)
                
        return render_template('submit_assignment.html',
                             subject=subject,
                             assignment=assignment)
                             
    except Exception as e:
        print(f"Error in submit_assignment: {str(e)}")
        flash('An error occurred while submitting the assignment.', 'danger')
        return redirect(url_for('student_dashboard'))

@app.route('/download_file/<file_id>')
@login_required
def download_file(file_id):
    if current_user.role != 'student':
        flash('Access denied. Student privileges required.', 'danger')
        return redirect(url_for('index'))
    
    try:
        print(f"Debug: Attempting to download file {file_id}")
        
        # Find the assignment containing this file
        subject = mongo.db.subjects.find_one({
            'assignments.files._id': ObjectId(file_id)
        })
        
        if not subject:
            print("Debug: No subject found with this file")
            flash('File not found.', 'danger')
            return redirect(url_for('student_dashboard'))
        
        # Find the file data
        file_data = None
        for assignment in subject.get('assignments', []):
            for file in assignment.get('files', []):
                if str(file.get('_id')) == str(file_id):
                    file_data = file
                    break
            if file_data:
                break
        
        if not file_data:
            print("Debug: File data not found")
            flash('File not found.', 'danger')
            return redirect(url_for('view_subject', subject_id=subject['_id']))
        
        # Get file from GridFS
        try:
            grid_file = fs.get(file_data['file_id'])
            print(f"Debug: Retrieved file from GridFS: {grid_file.filename}")
            
            # Create a file-like object from GridFS file
            file_content = io.BytesIO(grid_file.read())
            file_content.seek(0)
            
            return send_file(
                file_content,
                mimetype=grid_file.content_type,
                as_attachment=True,
                download_name=grid_file.filename
            )
            
        except Exception as e:
            print(f"Error retrieving file from GridFS: {str(e)}")
            flash('Error downloading file.', 'danger')
            return redirect(url_for('view_subject', subject_id=subject['_id']))
        
    except Exception as e:
        print(f"Debug: Error in download_file: {str(e)}")
        flash('An error occurred while downloading the file.', 'danger')
        return redirect(url_for('student_dashboard'))

@app.route('/view_assignment/<assignment_id>')
@login_required
def view_assignment(assignment_id):
    try:
        # Convert assignment_id to ObjectId
        assignment_id_obj = ObjectId(assignment_id)
        
        # Find the subject containing this assignment
        subject = mongo.db.subjects.find_one({
            'assignments._id': assignment_id_obj
        })

        print(f"Debug: Found subject: {subject['name'] if subject else 'None'}")

        if not subject:
            flash('Assignment not found.', 'danger')
            return redirect(url_for('student_dashboard'))

        # Find the specific assignment
        assignment = None
        for a in subject['assignments']:
            if a.get('_id') == assignment_id_obj:
                assignment = a
                break
                
        if not assignment:
            flash('Assignment not found.', 'danger')
            return redirect(url_for('student_dashboard'))

        # Check permissions
        if current_user.role == 'teacher':
            # Teachers can view all assignments they created
            if str(subject.get('teacher_id')) != current_user.id:
                flash('Access denied. You can only view assignments for your subjects.', 'danger')
                return redirect(url_for('teacher_dashboard'))
        else:
            # Students can only view assignments from subjects they're enrolled in
            is_enrolled = False
            for enrollment in subject.get('enrollments', []):
                if str(enrollment.get('student_id')) == str(current_user.id):
                    is_enrolled = True
                    break
                    
            if not is_enrolled:
                flash('Access denied. You must be enrolled in this subject.', 'danger')
                return redirect(url_for('student_dashboard'))
        
        # Get current time for deadline comparison
        current_time = datetime.utcnow()
        
        return render_template('view_assignment.html',
                             subject=subject,
                             assignment=assignment,
                             current_time=current_time)
    
    except Exception as e:
        print(f"Error in view_assignment: {str(e)}")
        flash('An error occurred while viewing the assignment.', 'danger')
        return redirect(url_for('student_dashboard'))

@app.template_global()
def get_student_submission(submissions, student_id):
    """Get a student's submission from a list of submissions."""
    return next(
        (sub for sub in submissions if str(sub.get('student_id')) == str(student_id)),
        None
    )

@app.route('/edit_assignment/<subject_id>/<assignment_id>', methods=['GET', 'POST'])
@login_required
def edit_assignment(subject_id, assignment_id):
    if current_user.role != 'teacher':
        flash('Access denied. Teacher privileges required.', 'danger')
        return redirect(url_for('student_dashboard'))
        
    try:
        # Convert IDs to ObjectId
        subject_id_obj = ObjectId(subject_id)
        assignment_id_obj = ObjectId(assignment_id)
        
        # Find subject
        subject = mongo.db.subjects.find_one({'_id': subject_id_obj})
        if not subject:
            flash('Subject not found.', 'danger')
            return redirect(url_for('teacher_dashboard'))
            
        # Check if teacher owns the subject
        if str(subject.get('teacher_id')) != str(current_user.id):
            flash('Access denied. You can only edit your own subjects.', 'danger')
            return redirect(url_for('teacher_dashboard'))
            
        # Find assignment
        assignment = None
        for a in subject.get('assignments', []):
            if a.get('_id') == assignment_id_obj:
                assignment = a
                break
                
        if not assignment:
            flash('Assignment not found.', 'danger')
            return redirect(url_for('manage_subject', subject_id=subject_id))
            
        if request.method == 'POST':
            title = request.form.get('title')
            description = request.form.get('description')
            due_date_str = request.form.get('due_date')
            points = request.form.get('points')
            
            if not all([title, description, due_date_str, points]):
                flash('Please fill in all required fields.', 'danger')
                return render_template('edit_assignment.html', subject=subject, assignment=assignment)
                
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M')
                points = int(points)
                
                # Update assignment
                result = mongo.db.subjects.update_one(
                    {
                        '_id': subject_id_obj,
                        'assignments._id': assignment_id_obj
                    },
                    {
                        '$set': {
                            'assignments.$.title': title,
                            'assignments.$.description': description,
                            'assignments.$.due_date': due_date,
                            'assignments.$.points': points,
                            'assignments.$.updated_at': datetime.utcnow()
                        }
                    }
                )
                
                if result.modified_count > 0:
                    flash('Assignment updated successfully!', 'success')
                    return redirect(url_for('manage_subject', subject_id=subject_id))
                else:
                    flash('No changes were made to the assignment.', 'info')
                    
            except ValueError:
                flash('Invalid date format or points value.', 'danger')
                
        return render_template('edit_assignment.html', subject=subject, assignment=assignment)
        
    except Exception as e:
        print(f"Error editing assignment: {str(e)}")
        flash('Error updating assignment.', 'danger')
        return redirect(url_for('manage_subject', subject_id=subject_id))

@app.route('/delete_assignment_file/<subject_id>/<assignment_id>/<file_id>', methods=['POST'])
@login_required
def delete_assignment_file(subject_id, assignment_id, file_id):
    if current_user.role != 'teacher':
        return jsonify({'success': False, 'message': 'Access denied'})
        
    try:
        # Find and remove the file from GridFS
        try:
            fs.delete(ObjectId(file_id))
            print(f"Debug: Deleted file {file_id} from GridFS")
        except Exception as e:
            print(f"Error deleting file from GridFS: {str(e)}")
            
        # Remove file reference from assignment
        result = mongo.db.subjects.update_one(
            {
                '_id': ObjectId(subject_id),
                'assignments._id': ObjectId(assignment_id)
            },
            {
                '$pull': {
                    'assignments.$.files': {
                        'file_id': ObjectId(file_id)
                    }
                }
            }
        )
        
        if result.modified_count > 0:
            return jsonify({
                'success': True,
                'message': 'File deleted successfully'
            })
        else:
            return jsonify({
                'success': False,
                'message': 'File not found'
            })
            
    except Exception as e:
        print(f"Error deleting assignment file: {str(e)}")
        return jsonify({
            'success': False,
            'message': 'Error deleting file'
        })

@app.route('/grade_submission/<submission_id>', methods=['POST'])
@login_required
def grade_submission(submission_id):
    if current_user.role != 'teacher':
        return jsonify({'success': False, 'message': 'Access denied. Teacher privileges required.'})
    
    try:
        print(f"Received submission_id: {submission_id}")
        
        # Basic validation
        if not submission_id or not isinstance(submission_id, str):
            return jsonify({'success': False, 'message': 'Invalid submission ID format'})
        
        # Clean the submission ID (remove any whitespace or quotes)
        submission_id = submission_id.strip().strip('"\'')
        
        # Validate ObjectId format
        if not ObjectId.is_valid(submission_id):
            return jsonify({'success': False, 'message': f'Invalid submission ID format: {submission_id}'})
            
        # Convert submission_id to ObjectId
        submission_id_obj = ObjectId(submission_id)
        
        # Get and validate request data
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data received'})
            
        grade = data.get('grade')
        feedback = data.get('feedback', '')
        
        # Validate grade
        if grade is None:
            return jsonify({'success': False, 'message': 'Grade is required'})
            
        try:
            grade = int(grade)
        except ValueError:
            return jsonify({'success': False, 'message': 'Grade must be a number'})
        
        # Find the subject and assignment containing this submission
        print(f"Looking for submission with ID: {submission_id_obj}")
        subject = mongo.db.subjects.find_one({
            "assignments.submissions._id": submission_id_obj
        })
        
        if not subject:
            print(f"No subject found with submission ID: {submission_id_obj}")
            return jsonify({'success': False, 'message': 'Submission not found'})
            
        # Make sure the teacher owns this subject
        if str(subject.get('teacher_id')) != str(current_user.id):
            return jsonify({'success': False, 'message': 'Access denied. You can only grade submissions for your subjects.'})
        
        # Update the submission with grade and feedback
        result = mongo.db.subjects.update_one(
            {
                "assignments.submissions._id": submission_id_obj
            },
            {
                "$set": {
                    "assignments.$[].submissions.$[sub].grade": grade,
                    "assignments.$[].submissions.$[sub].feedback": feedback,
                    "assignments.$[].submissions.$[sub].graded_at": datetime.utcnow(),
                    "assignments.$[].submissions.$[sub].graded_by": current_user.id
                }
            },
            array_filters=[{"sub._id": submission_id_obj}]
        )
        
        if result.modified_count > 0:
            print(f"Successfully updated submission {submission_id_obj} with grade {grade}")
            return jsonify({
                'success': True,
                'message': 'Grade saved successfully!',
                'grade': grade,
                'feedback': feedback
            })
        else:
            print(f"No submission was updated for ID: {submission_id_obj}")
            return jsonify({'success': False, 'message': 'No submission was updated. Please try again.'})
            
    except Exception as e:
        print(f"Error grading submission: {str(e)}")
        return jsonify({'success': False, 'message': f'An error occurred while saving the grade: {str(e)}'})

@app.template_global()
def get_student_name(student_id):
    """Get a student's name from their ID."""
    try:
        if not student_id:
            return 'Unknown Student'
            
        # Convert string ID to ObjectId if needed
        if isinstance(student_id, str):
            student_id = ObjectId(student_id)
        elif isinstance(student_id, ObjectId):
            pass
        else:
            return 'Unknown Student'
            
        student = mongo.db.users.find_one({'_id': student_id})
        if student:
            first_name = student.get('first_name', '')
            last_name = student.get('last_name', '')
            if first_name or last_name:
                return f"{first_name} {last_name}".strip()
            return student.get('username', 'Unknown Student')
        return 'Unknown Student'
    except Exception as e:
        print(f"Error getting student name: {str(e)}")
        return 'Unknown Student'

@app.route('/subject/<subject_id>/chat')
@login_required
def subject_chat(subject_id):
    try:
        subject = mongo.db.subjects.find_one({'_id': ObjectId(subject_id)})
        if not subject:
            flash('Subject not found')
            return redirect(url_for('index'))
        
        # Get chat history for this subject
        chat_history = list(mongo.db.subject_messages.find({
            'subject_id': ObjectId(subject_id)
        }).sort('timestamp', 1))
        
        return render_template('subject_chat.html', subject=subject, chat_history=chat_history)
        
    except Exception as e:
        print(f"Debug: Error in subject_chat: {str(e)}")
        flash('An error occurred while accessing the chat.', 'danger')
        return redirect(url_for('index'))

@socketio.on('join_subject')
def on_join_subject(data):
    subject_id = data['subject_id']
    room = f'subject_{subject_id}'
    join_room(room)

@socketio.on('leave_subject')
def on_leave_subject(data):
    subject_id = data['subject_id']
    room = f'subject_{subject_id}'
    leave_room(room)

@socketio.on('send_subject_message')
def handle_subject_message(data):
    subject_id = data['subject_id']
    message = data['message']
    room = f'subject_{subject_id}'
    
    # Save message to database
    message_data = {
        'subject_id': ObjectId(subject_id),
        'sender_id': ObjectId(current_user.id),
        'sender_name': current_user.username,
        'sender_role': current_user.role,
        'message': message,
        'timestamp': datetime.utcnow()
    }
    mongo.db.subject_messages.insert_one(message_data)
    
    # Emit message to room
    emit('new_subject_message', {
        'sender_id': current_user.id,
        'sender_name': current_user.username,
        'sender_role': current_user.role,
        'message': message,
        'timestamp': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    }, room=room)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5999, allow_unsafe_werkzeug=True)
