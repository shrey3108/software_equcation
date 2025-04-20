# Education Platform

A Flask-based web application for managing student-teacher interactions, including subject management, quizzes, and AI-assisted learning.

## Features

- User registration and authentication (Students and Teachers)
- Teacher features:
  - Create and manage subjects
  - Create quizzes
  - Track student progress
  - View enrolled students
- Student features:
  - View enrolled subjects
  - Take quizzes
  - Track progress
  - AI assistant for learning help

## Setup

1. Install the required packages:
```bash
pip install -r requirements.txt
```

2. Set up environment variables:
Create a `.env` file with the following content:
```
GOOGLE_API_KEY=your_google_api_key_here
```

3. Run the application:
```bash
python app.py
```

The application will be available at `http://localhost:5000`

## Usage

1. Register as either a student or teacher
2. Login with your credentials
3. Access your dashboard:
   - Teachers can create subjects and quizzes
   - Students can view subjects, take quizzes, and use the AI assistant


