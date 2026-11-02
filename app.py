import os
import re
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

import requests
import mysql.connector

from dotenv import load_dotenv
from mysql.connector import Error
from flask import (
    Flask, render_template, request,
    redirect, url_for, send_file
)
from flask_wtf.csrf import CSRFProtect

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable
)

# --------------------------------------------------
# 1. Configuration
# --------------------------------------------------

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv(
    "FLASK_SECRET_KEY",
    "dev-secret-key-change-in-production"
)

csrf = CSRFProtect(app)

API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/auto")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# --------------------------------------------------
# 2. MySQL connection
# --------------------------------------------------

APP_TRIP_HISTORY = []
APP_TRIP_COUNTER = 1


def get_db_connection():
    try:
        return mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "localhost"),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "travel_planner"),
            connection_timeout=5
        )
    except (Error, mysql.connector.DatabaseError, mysql.connector.InterfaceError) as error:
        print("MySQL unavailable:", error)
        return None


def add_memory_trip(destination, travel_plan):
    global APP_TRIP_COUNTER

    trip = {
        "id": APP_TRIP_COUNTER,
        "destination": destination,
        "travel_plan": travel_plan,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    }
    APP_TRIP_COUNTER += 1
    APP_TRIP_HISTORY.insert(0, trip)
    return True


# --------------------------------------------------
# 3. Save travel plan
# --------------------------------------------------

def save_trip(destination, travel_plan):
    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        if connection is None:
            return add_memory_trip(destination, travel_plan)

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO travel_history (destination, travel_plan)
            VALUES (%s, %s)
            """,
            (destination, travel_plan)
        )

        connection.commit()
        return True

    except Error as error:
        print("Database save error:", error)
        return add_memory_trip(destination, travel_plan)

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()


# --------------------------------------------------
# 4. Generate AI travel plan
# --------------------------------------------------

def generate_travel_plan(
    destination, budget, days, travel_style, persons
):
    prompt = f"""
You are a helpful AI travel planning assistant.

Create a practical travel plan using these details:

Destination or route: {destination}
Total trip budget in INR: {budget if budget else "Not specified"}
Trip duration in days: {days if days else "Not specified"}
Travel style: {travel_style}
Number of travellers: {persons}

IMPORTANT REQUIREMENTS:

1. Treat the entered budget as the TOTAL budget for the whole group.
2. Plan the trip for exactly {persons} traveller(s).
3. Show the estimated total group cost and estimated cost per person.
4. Include accommodation, food, transport, activities and miscellaneous
   expenses for the entire group.
5. Clearly state assumptions about rooms, sharing and transport.
6. If the budget is too low, explain this and suggest adjustments.
7. If trip duration is specified, create a day-wise itinerary.
8. Include attractions, activities, food and transport options.
9. Keep expense calculations internally consistent and use INR.
10. For routes between cities, mention approximate distance and journey
    time where appropriate.
11. Do not invent live ticket prices, hotel availability, schedules or
    real-time road conditions.
12. Clearly label costs, distances and journey times as estimates.
13. Use clear Markdown headings, bold text and bullet points.
14. If details are missing, state reasonable assumptions.

Suggested response structure:

# Your Travel Plan
## Trip Overview
## Day-wise Itinerary
## Estimated Budget Breakdown
## Transport Options
## Food and Accommodation
## Practical Travel Tips
## Budget Summary
"""

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.7
        },
        timeout=90
    )

    response.raise_for_status()

    data = response.json()
    content = data["choices"][0]["message"]["content"]

    if not isinstance(content, str) or not content.strip():
        raise ValueError("The AI returned an empty travel plan.")

    return content.strip()


# --------------------------------------------------
# 5. Home page and trip generation
# --------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    result = ""
    destination = ""
    budget = ""
    days = ""
    travel_style = "Standard"
    persons = "1"

    if request.method == "POST":
        destination = request.form.get("city", "").strip()
        budget = request.form.get("budget", "").strip()
        days = request.form.get("days", "").strip()
        travel_style = request.form.get(
            "travel_style", "Standard"
        ).strip()
        persons = request.form.get("persons", "1").strip()

        # Validate destination
        if not destination:
            result = "Please enter a destination or travel route."

        elif len(destination) > 200:
            result = "Destination must be 200 characters or fewer."

        # Validate budget
        if not result and budget:
            try:
                budget_value = float(budget)

                if not 1 <= budget_value <= 10000000:
                    raise ValueError

                budget = str(budget_value)

            except ValueError:
                result = "Enter a valid budget between ₹1 and ₹1 crore."

        # Validate trip duration
        if not result and days:
            try:
                days_value = int(days)

                if not 1 <= days_value <= 60:
                    raise ValueError

                days = str(days_value)

            except ValueError:
                result = "Trip duration must be between 1 and 60 days."

        # Validate number of travellers
        if not result:
            try:
                persons_value = int(persons)

                if not 1 <= persons_value <= 50:
                    raise ValueError

                persons = str(persons_value)

            except ValueError:
                result = "Number of travellers must be between 1 and 50."

        # Validate travel style
        if not result and travel_style not in (
            "Budget", "Standard", "Luxury"
        ):
            result = "Please select a valid travel style."

        # Check API key
        if not result and not API_KEY:
            result = "API key is missing. Please check your .env file."

        # Generate and save plan
        if not result:
            try:
                result = generate_travel_plan(
                    destination=destination,
                    budget=budget,
                    days=days,
                    travel_style=travel_style,
                    persons=persons
                )

                saved = save_trip(destination, result)

                if not saved:
                    print(
                        "Travel plan generated, but MySQL save failed."
                    )

            except requests.Timeout:
                print("OpenRouter request timed out.")
                result = (
                    "The AI request took too long. Please try again."
                )

            except requests.RequestException as error:
                print("OpenRouter request failed:", error)
                result = (
                    "Unable to generate your travel plan. Check your "
                    "internet, API key, account credits, model "
                    "availability and try again."
                )

            except (KeyError, IndexError, TypeError, ValueError) as error:
                print("AI response error:", error)
                result = (
                    "The AI service returned an unexpected response. "
                    "Please try again."
                )

    return render_template(
        "index.html",
        result=result,
        destination=destination,
        budget=budget,
        days=days,
        travel_style=travel_style,
        persons=persons
    )


# --------------------------------------------------
# 6. Travel history
# --------------------------------------------------

@app.route("/history")
def history():
    trips = []
    db_error = False

    connection = get_db_connection()

    if connection is None:
        trips = list(APP_TRIP_HISTORY)
    else:
        cursor = None

        try:
            cursor = connection.cursor(dictionary=True)

            cursor.execute(
                """
                SELECT id, destination, travel_plan, created_at
                FROM travel_history
                ORDER BY created_at DESC
                """
            )

            trips = cursor.fetchall()

        except Error as error:
            print("Database history error:", error)
            trips = list(APP_TRIP_HISTORY)
            db_error = True

        finally:
            if cursor:
                cursor.close()

            if connection and connection.is_connected():
                connection.close()

    return render_template(
        "history.html",
        trips=trips,
        db_error=db_error
    )


@app.route("/delete-trip/<int:trip_id>", methods=["POST"])
def delete_trip(trip_id):
    connection = None
    cursor = None

    try:
        connection = get_db_connection()

        if connection is None:
            APP_TRIP_HISTORY[:] = [trip for trip in APP_TRIP_HISTORY if trip["id"] != trip_id]
            return redirect(url_for("history"))

        cursor = connection.cursor()

        cursor.execute(
            "DELETE FROM travel_history WHERE id = %s",
            (trip_id,)
        )

        connection.commit()

    except Error as error:
        print("Database delete error:", error)
        APP_TRIP_HISTORY[:] = [trip for trip in APP_TRIP_HISTORY if trip["id"] != trip_id]

    finally:
        if cursor:
            cursor.close()

        if connection and connection.is_connected():
            connection.close()

    return redirect(url_for("history"))


@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    destination = request.form.get("destination", "").strip()
    travel_plan = request.form.get("travel_plan", "").strip()

    if not destination or not travel_plan:
        return "Destination or travel plan is missing.", 400

    if len(destination) > 200 or len(travel_plan) > 30000:
        return "The travel plan is too long to export.", 400

    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=48,
        leftMargin=48,
        topMargin=48,
        bottomMargin=48,
        title=f"Voyara Travel Plan - {destination}",
        author="Voyara AI Travel Planner",
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "VoyaraTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=28,
        textColor=colors.HexColor("#244b40"),
        alignment=TA_CENTER,
        spaceAfter=10,
    )

    heading_style = ParagraphStyle(
        "VoyaraHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=17,
        textColor=colors.HexColor("#244b40"),
        spaceBefore=12,
        spaceAfter=6,
    )

    body_style = ParagraphStyle(
        "VoyaraBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor("#39413b"),
        spaceAfter=5,
    )

    bullet_style = ParagraphStyle(
        "VoyaraBullet",
        parent=body_style,
        leftIndent=12,
        firstLineIndent=-8,
        spaceAfter=4,
    )

    story = [
        Paragraph("VOYARA", title_style),
        Paragraph(
            f"Your Travel Plan: {escape(destination)}",
            heading_style
        ),
        HRFlowable(
            width="100%",
            thickness=1,
            color=colors.HexColor("#df795e")
        ),
        Spacer(1, 12),
    ]

    for raw_line in travel_plan.splitlines():
        line = raw_line.strip()

        if not line:
            story.append(Spacer(1, 4))
            continue

        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            story.append(Paragraph(escape(heading), heading_style))
            continue

        if line.startswith(("- ", "* ", "+ ")):
            item = line[2:].strip()
            item = re.sub(r"\*\*(.*?)\*\*", r"\1", item)
            item = re.sub(r"(?<!\*)\*([^*]+)\*", r"\1", item)
            item = re.sub(r"`([^`]+)`", r"\1", item)

            story.append(
                Paragraph(f"&bull; {escape(item)}", bullet_style)
            )
            continue

        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"(?<!\*)\*([^*]+)\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)

        story.append(Paragraph(escape(line), body_style))

    story.extend([
        Spacer(1, 14),
        HRFlowable(
            width="100%",
            thickness=0.5,
            color=colors.HexColor("#e7e4dc")
        ),
        Spacer(1, 8),
        Paragraph(
            "Note: Travel costs, schedules, distances and availability "
            "are estimates unless independently verified.",
            styles["Italic"],
        ),
    ])

    document.build(story)
    buffer.seek(0)

    safe_name = re.sub(
        r"[^A-Za-z0-9_-]+", "-", destination
    ).strip("-")[:50]

    filename = f"voyara-{safe_name or 'travel-plan'}.pdf"

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


# --------------------------------------------------
# 9. Run application
# --------------------------------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)