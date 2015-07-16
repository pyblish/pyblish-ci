FROM ubuntu:14.04

MAINTAINER marcus@abstractfactory.io

RUN apt-get install -y \
  libffi6 \
  libffi-dev \
  libssl-dev \
  build-essential \
  python-dev \
  nano \
  wget
