pipeline {
    agent any

    environment {
        PYTHON_VERSION = '3.11'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Setup') {
            steps {
                sh '''
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install --upgrade pip
                    pip install -e ".[secure,semantic]"
                    pip install pytest pytest-cov
                '''
            }
        }

        stage('Test') {
            steps {
                sh '''
                    . .venv/bin/activate
                    pytest tests/ --cov=src/clipmcp --cov-report=xml --cov-report=term
                '''
            }
            post {
                always {
                    junit 'test-results/*.xml'        // if you add pytest-junit
                    cobertura coberturaReportFile: 'coverage.xml'
                }
            }
        }

        stage('Build') {
            steps {
                sh '''
                    . .venv/bin/activate
                    pip install hatch
                    hatch build
                '''
            }
        }
    }

    post {
        always {
            cleanWs()
        }
        failure {
            echo 'Build failed!'
        }
    }
}