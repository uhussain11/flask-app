# Use an official Python runtime as a parent image
FROM python:3.8-slim

# Set the working directory in the container
WORKDIR /usr/src/app

# Copy the requirements file into the container
COPY requirements.txt ./

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container
COPY . .

# Define environment variable to ensure output is flushed straight to the terminal
ENV PYTHONUNBUFFERED=1

# Expose port 80 to allow external access
EXPOSE 80

# Run the main script when the container launches
CMD ["python", "app.py"]
