from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from flask_pymongo import PyMongo
from bson.objectid import ObjectId
import os
from datetime import datetime
import builtins
from config import GEMINI_API_KEY, MONGO_URI, SECRET_KEY, YOUTUBE_API_KEY
import google.generativeai as genai
import json
import requests
from googleapiclient.discovery import build
import urllib.parse

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MONGO_URI'] = MONGO_URI
app.config['GEMINI_API_KEY'] = GEMINI_API_KEY

# Configure Gemini AI
genai.configure(api_key=app.config['GEMINI_API_KEY'])

# YouTube API Configuration
YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"

def search_youtube_videos(query):
    try:
        youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
        
        # Call the search.list method with more specific parameters
        search_response = youtube.search().list(
            q=query,
            part='id,snippet',
            maxResults=6,
            type='video',
            relevanceLanguage='en',
            videoDefinition='high',
            order='relevance',  # Use relevance ordering
            videoDuration='medium'  # Get medium length videos (4-20 mins)
        ).execute()

        videos = []
        for search_result in search_response.get('items', []):
            if search_result['id']['kind'] == 'youtube#video':
                video = {
                    'id': search_result['id']['videoId'],
                    'title': search_result['snippet']['title'],
                    'description': search_result['snippet']['description'],
                    'thumbnail': search_result['snippet']['thumbnails']['medium']['url']
                }
                videos.append(video)

        return videos
    except Exception as e:
        print(f"YouTube API error: {str(e)}")
        return []

mongo = PyMongo(app)




@app.route('/')
def index():
    return redirect(url_for('student_dashboard'))

@app.route('/student_dashboard')
def student_dashboard():
    try:
        # Get all learning paths, sorted by most recently updated
        learning_paths = list(mongo.db.learning_paths.find().sort('last_updated', -1))
        
        # Convert ObjectId to string for each path
        for path in learning_paths:
            path['_id'] = str(path['_id'])
        
        # Get all subjects
        subjects = list(mongo.db.subjects.find())
        
        # Add schedule information to subjects if not present
        for subject in subjects:
            if 'schedule' not in subject:
                subject['schedule'] = {
                    'day': 'Not set',
                    'start_time': 'Not set',
                    'end_time': 'Not set'
                }
        
        return render_template('student_dashboard.html',
                             learning_paths=learning_paths,
                             subjects=subjects,
                             zip=builtins.zip)
                             
    except Exception as e:
        print(f"Error in student_dashboard: {str(e)}")
        flash('Error loading dashboard. Please try again.', 'error')
        return render_template('student_dashboard.html', 
                             learning_paths=[],
                             subjects=[])

@app.route('/personalized_learning')
def personalized_learning():
    learning_paths = list(mongo.db.learning_paths.find())
    
    return render_template('personalized_learning.html', learning_paths=learning_paths)

@app.route('/create_custom_path', methods=['POST'])
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
            'subject_title': subject_title,
            'current_knowledge': current_knowledge,
            'time_commitment': time_commitment,
            'preferred_resources': resources,
            'modules': chapters,
            'progress': 0,
            'created_at': datetime.utcnow(),
            'last_updated': datetime.utcnow(),
            'description': f'A personalized learning path for {subject_title}'  # Added description
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
def continue_learning(learning_path_id):
    try:
        # Get the learning path
        learning_path = mongo.db.learning_paths.find_one({
            '_id': ObjectId(learning_path_id)
        })
        
        if not learning_path:
            flash('Learning path not found')
            return redirect(url_for('student_dashboard'))

        return render_template('continue_learning.html', 
                            learning_path=learning_path)

    except Exception as e:
        print(f"Error in continue_learning: {str(e)}")
        flash('Error accessing learning path')
        return redirect(url_for('student_dashboard'))

@app.route('/view_learning_path/<path_id>')
def view_learning_path(path_id):
    try:
        # Get the learning path
        learning_path = mongo.db.learning_paths.find_one({
            '_id': ObjectId(path_id)
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
def delete_learning_path(path_id):
    try:
        # Convert string ID to ObjectId
        path_id = ObjectId(path_id)
        
        # Find and delete the learning path
        result = mongo.db.learning_paths.delete_one({'_id': path_id})
        
        if result.deleted_count > 0:
            flash('Learning path deleted successfully!')
            return jsonify({'success': True, 'message': 'Learning path deleted successfully'})
        else:
            return jsonify({'success': False, 'message': 'Learning path not found'}), 404
            
    except Exception as e:
        print(f"Error deleting learning path: {str(e)}")
        return jsonify({'success': False, 'message': 'Error deleting learning path'}), 500




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
def get_module_videos():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject')
        
        if not module or not subject:
            return jsonify([])

        # Search for relevant videos
        videos = search_youtube_videos(f"{subject} {module}")
        return jsonify(videos)

    except Exception as e:
        print(f"Error fetching videos: {str(e)}")
        return jsonify({'error': 'Failed to fetch videos'}), 500

@app.route('/get_module_docs')
def get_module_docs():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject')
        
        if not module or not subject:
            return jsonify([])

        # Get documentation from Gemini
        docs = get_resources_from_gemini(module, subject)
        if not docs:
            docs = get_fallback_resources(module, subject)
            
        return jsonify(docs.get('documentation', []))

    except Exception as e:
        print(f"Error fetching documentation: {str(e)}")
        return jsonify({'error': 'Failed to fetch documentation'}), 500

@app.route('/get_module_articles')
def get_module_articles():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject')
        
        if not module or not subject:
            return jsonify([])

        # Get articles from Gemini
        articles = get_resources_from_gemini(module, subject).get('articles', [])
        if not articles:
            articles = get_fallback_resources(module, subject).get('articles', [])
            
        return jsonify(articles)

    except Exception as e:
        print(f"Error fetching articles: {str(e)}")
        return jsonify({'error': 'Failed to fetch articles'}), 500

@app.route('/get_module_quiz')
def get_module_quiz():
    try:
        module = request.args.get('module')
        subject = request.args.get('subject')
        
        if not module or not subject:
            return jsonify({'questions': []})

        # Generate quiz using Gemini
        model = genai.GenerativeModel('gemini-pro')
        prompt = f"""
        Create a quiz about {module} in {subject}.
        Return the response in this exact JSON format:
        {{
            "questions": [
                {{
                    "text": "What is ...",
                    "options": ["option1", "option2", "option3", "option4"],
                    "correct": 0
                }}
            ]
        }}
        Create 5 multiple-choice questions. Make them educational and cover key concepts.
        Each question should have exactly 4 options.
        The 'correct' field should be the index (0-3) of the correct option.
        """
        
        response = model.generate_content(prompt)
        quiz_text = response.text
        
        # Clean up the response if needed
        if '```json' in quiz_text:
            quiz_text = quiz_text.split('```json')[1].split('```')[0]
        
        quiz_data = json.loads(quiz_text.strip())
        return jsonify(quiz_data)

    except Exception as e:
        print(f"Error generating quiz: {str(e)}")
        # Return a fallback quiz if Gemini fails
        fallback_quiz = {
            'questions': [
                {
                    'text': f'What is the main purpose of {module}?',
                    'options': [
                        f'To understand {module} concepts',
                        f'To apply {module} in practice',
                        f'To solve real-world problems',
                        'All of the above'
                    ],
                    'correct': 3
                },
                {
                    'text': f'Which is a key component of {module}?',
                    'options': [
                        'Theory',
                        'Practice',
                        'Application',
                        'All of the above'
                    ],
                    'correct': 3
                }
            ]
        }
        return jsonify(fallback_quiz)

def get_resources_from_gemini(module_name, subject):
    """Use Gemini to find relevant learning resources for any subject"""
    try:
        # Initialize Gemini model
        model = genai.GenerativeModel('gemini-pro')
        
        # Generate documentation with links
        docs_prompt = f"""
        Create documentation about {module_name} in {subject}.
        Return a JSON array with exactly 2 sections:
        [
            {{
                "title": "Getting Started with {module_name}",
                "content": "<detailed beginner-friendly explanation>",
                "links": [
                    {{
                        "title": "Official Documentation",
                        "url": "https://docs.python.org/3/library/{module_name.lower()}.html",
                        "description": "Official Python documentation"
                    }},
                    {{
                        "title": "Tutorial on GeeksforGeeks",
                        "url": "https://www.geeksforgeeks.org/python-{module_name.lower()}/",
                        "description": "Step-by-step tutorial with examples"
                    }}
                ]
            }},
            {{
                "title": "Advanced Topics in {module_name}",
                "content": "<detailed explanation of advanced concepts>",
                "links": [
                    {{
                        "title": "Real Python Tutorial",
                        "url": "https://realpython.com/search?q={module_name.lower()}",
                        "description": "In-depth guide with practical examples"
                    }},
                    {{
                        "title": "W3Schools Reference",
                        "url": "https://www.w3schools.com/python/{module_name.lower()}.asp",
                        "description": "Quick reference and examples"
                    }}
                ]
            }}
        ]
        Make sure:
        1. Content is educational and includes examples
        2. Links are relevant and working
        3. Each section has exactly 2 links
        """
        
        # Get response from Gemini
        docs_response = model.generate_content(docs_prompt)
        docs_text = docs_response.text
        
        # Clean up response if needed
        if '```json' in docs_text:
            docs_text = docs_text.split('```json')[1].split('```')[0]
        
        # Parse documentation
        documentation = json.loads(docs_text.strip())
        
        # Add default links if none provided
        for section in documentation:
            if 'links' not in section or not section['links']:
                section['links'] = [
                    {
                        'title': f'{module_name} Documentation',
                        'url': f'https://www.geeksforgeeks.org/python-{module_name.lower()}/',
                        'description': 'Comprehensive guide and examples'
                    },
                    {
                        'title': f'Learn {module_name}',
                        'url': f'https://www.w3schools.com/python/{module_name.lower()}.asp',
                        'description': 'Interactive tutorial and reference'
                    }
                ]
        
        return {
            'documentation': documentation,
            'articles': []  # We'll handle articles separately
        }
        
    except Exception as e:
        print(f"Error in get_resources_from_gemini: {str(e)}")
        return get_fallback_resources(module_name, subject)

def get_fallback_resources(module_name, subject):
    """Fallback function to get basic resources if Gemini fails"""
    module_slug = module_name.lower().replace(' ', '-')
    return {
        'documentation': [
            {
                'title': f'Getting Started with {module_name}',
                'content': f'''
                    <h3>Overview</h3>
                    <p>This module covers the fundamentals of {module_name} in {subject}.</p>
                    
                    <h3>Learning Objectives</h3>
                    <ul>
                        <li>Understand basic concepts of {module_name}</li>
                        <li>Learn practical applications</li>
                        <li>Master key techniques</li>
                    </ul>
                    
                    <h3>Getting Started</h3>
                    <p>We'll begin by exploring the core concepts and gradually move to more advanced topics.</p>
                ''',
                'links': [
                    {
                        'title': 'GeeksforGeeks Tutorial',
                        'url': f'https://www.geeksforgeeks.org/python-{module_slug}/',
                        'description': 'Step-by-step tutorial with examples'
                    },
                    {
                        'title': 'W3Schools Guide',
                        'url': f'https://www.w3schools.com/python/{module_slug}.asp',
                        'description': 'Interactive learning with examples'
                    }
                ]
            },
            {
                'title': 'Advanced Concepts',
                'content': f'''
                    <h3>Core Principles</h3>
                    <p>The main principles of {module_name} include:</p>
                    <ul>
                        <li>Fundamental concepts</li>
                        <li>Practical applications</li>
                        <li>Best practices</li>
                    </ul>
                    
                    <h3>Common Use Cases</h3>
                    <p>Here are some common applications of {module_name}:</p>
                    <ul>
                        <li>Real-world examples</li>
                        <li>Industry applications</li>
                        <li>Practical scenarios</li>
                    </ul>
                ''',
                'links': [
                    {
                        'title': 'Real Python Tutorial',
                        'url': f'https://realpython.com/search?q={module_slug}',
                        'description': 'In-depth guide with real-world examples'
                    },
                    {
                        'title': 'Python Documentation',
                        'url': f'https://docs.python.org/3/search.html?q={module_slug}',
                        'description': 'Official Python documentation and reference'
                    }
                ]
            }
        ],
        'articles': [
            {
                'title': f'Getting Started with {module_name}',
                'description': f'A beginner-friendly introduction to {module_name} concepts.',
                'url': f'https://www.geeksforgeeks.org/python-{module_slug}/'
            },
            {
                'title': f'Advanced {module_name} Techniques',
                'description': f'Deep dive into advanced {module_name} topics.',
                'url': f'https://realpython.com/search?q={module_slug}'
            }
        ]
    }

@app.route('/submit_quiz', methods=['POST'])
def submit_quiz():
    try:
        data = request.json
        if not data or 'answers' not in data:
            return jsonify({'error': 'Invalid quiz submission'}), 400

        # Calculate score (implement your scoring logic here)
        score = 75  # Example score
        
        return jsonify({
            'success': True,
            'score': score,
            'message': f'Quiz submitted successfully! Your score: {score}%'
        })

    except Exception as e:
        print(f"Error submitting quiz: {str(e)}")
        return jsonify({'error': 'Failed to submit quiz'}), 500

if __name__ == '__main__':
    app.run(debug=True , port = "5345")
