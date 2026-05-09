#!/bin/bash
set -e

# --- 1. Dependencies ---
sudo apt-get update
sudo apt-get install -y \
    git curl wget build-essential cmake automake libtool pkg-config \
    python3 python3-pip python3-venv python3-dev \
    libpcap-dev libgflags-dev libgtest-dev \
    libjudy-dev libboost-all-dev libnanomsg-dev \
    libevent-dev libreadline-dev libssl-dev \
    libthrift-dev thrift-compiler \
    flex bison libfl-dev libgc-dev \
    tshark iperf3 net-tools iproute2 \
    openvswitch-switch tcpdump htop jq nginx

INSTALL_DIR="/opt/p4"
NPROC=$(nproc)
sudo mkdir -p $INSTALL_DIR
sudo chown -R $USER:$USER $INSTALL_DIR

# --- 2. Build PI (P4Runtime Implementation) ---
# (Crucial: BMv2 needs these headers)
cd $INSTALL_DIR
if [ ! -d "PI" ]; then
    git clone --recursive https://github.com/p4lang/PI.git
fi
cd PI
./autogen.sh && ./configure --with-proto --without-internal-rpc --without-cli
make -j$NPROC && sudo make install && sudo ldconfig

# --- 3. Build BMv2 (The Switch) ---
cd $INSTALL_DIR
if [ ! -d "behavioral-model" ]; then
    git clone https://github.com/p4lang/behavioral-model.git
fi
cd behavioral-model
./install_deps.sh
./autogen.sh && ./configure --with-pi --with-thrift
make -j$NPROC && sudo make install && sudo ldconfig

# --- 4. Build p4c (The Compiler) ---
# We skip the PPA and build from source for 22.04 compatibility
cd $INSTALL_DIR
if [ ! -d "p4c" ]; then
    git clone --recursive https://github.com/p4lang/p4c.git
fi
cd p4c && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=RelWithDebInfo
make -j$NPROC && sudo make install && sudo ldconfig

# --- 5. Mininet ---
cd $INSTALL_DIR
if [ ! -d "mininet" ]; then
    git clone https://github.com/mininet/mininet.git
fi
cd mininet
sudo ./util/install.sh -nfv

echo "Installation Complete!"
