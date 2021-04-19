from setuptools import setup, find_packages

with open('README.md', 'r') as f:
    readme = f.read()

requirements = ['pandas>=1.2.0', 'pmdarima>=1.8.0', 'tbats>-1.1.0', 'statsmodels>=0.12.2']

setup(
    name="auto-ts",
    version="0.1",
    author="Andrew Walker",
    author_email="awalker88@me.com",
    description="Automated time-series forecasting",
    long_description=readme,
    long_description_content_type="text/markdown",
    url="https://github.com/awalker88/auto-ts",
    packages=find_packages(),
    install_requires=requirements,
    classifiers=[
        "Programming Language :: Python :: 3 :: Only",
        "License :: OSI Approved :: MIT License",
        "Development Status :: 4 - Beta"
    ],
)