FROM python:3.11-slim

# Create a user to avoid running as root
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# Install dependencies
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy the bot code
COPY --chown=user . .

# Hugging Face Spaces listen on port 7860
EXPOSE 7860

# Run the bot
CMD ["python", "bot.py"]
