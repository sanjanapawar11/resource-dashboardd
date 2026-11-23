from sqlalchemy import Column, Integer, BigInteger, String, Text, Date
from database import Base


class Employee(Base):
    __tablename__ = "employees"

    id = Column(BigInteger, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, nullable=True)
    technology = Column(String(100), nullable=False, default="")
    experience = Column(Integer, nullable=False, default=0)
    profile_data = Column(Text, nullable=False, default="{}")


class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(BigInteger, nullable=False)
    task_title = Column(String(200), nullable=False)
    task_details = Column(Text, nullable=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    link = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "dashboard_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(20), nullable=False)
