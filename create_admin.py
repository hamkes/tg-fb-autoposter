"""Create (or promote) an admin account from the CLI.

Usage:  python create_admin.py <email> <password>
"""
import sys

from app import app
from models import User


def main():
    if len(sys.argv) < 3:
        print("Usage: python create_admin.py <email> <password>")
        sys.exit(1)
    email = sys.argv[1].strip().lower()
    password = sys.argv[2]

    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, role="admin", is_active=True)
            user.set_password(password)
            from extensions import db

            db.session.add(user)
            db.session.commit()
            print(f"Admin '{email}' created.")
        else:
            user.role = "admin"
            user.is_active = True
            user.set_password(password)
            from extensions import db

            db.session.commit()
            print(f"'{email}' promoted to admin (password updated).")


if __name__ == "__main__":
    main()