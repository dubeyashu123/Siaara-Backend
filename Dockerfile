# Base Image: एक स्वच्छ Python 3.12 वातावरण से शुरू करें
FROM python:3.12-slim

# Working Directory: कंटेनर के अंदर /app फ़ोल्डर को वर्किंग डायरेक्टरी सेट करें
WORKDIR /app

# Dependencies इंस्टॉल करें: requirements.txt को कॉपी करें और pip install चलाएँ
# --no-cache-dir यह सुनिश्चित करता है कि यह VM के पुराने cache का उपयोग न करे
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App Code कॉपी करें: अपनी पूरी प्रोजेक्ट डायरेक्टरी को कंटेनर में कॉपी करें
COPY . .

# Port Expose करें: कंटेनर पोर्ट 8000 पर लिसन करेगा
EXPOSE 8000

# Container को चलाने के लिए कमांड (आपका मूल Gunicorn कमांड)
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "src.main:app", "-k", "uvicorn.workers.UvicornWorker"]