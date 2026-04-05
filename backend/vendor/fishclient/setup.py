# setup.py

from setuptools import setup, find_packages

setup(
    name="fishclient",
    version="0.1.4",
    packages=find_packages(),
    install_requires=[
        "requests",
        "msgpack",
        "websockets",
    ],
    author="pluhian",
    description="client for interacting with FishTank.live",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/pluhian/fishclient", 
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
    ],
    python_requires=">=3.7",
)
