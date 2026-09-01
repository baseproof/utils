# Build targets for the WireGuard egress fleet.
ZONE ?= us-central1-a
VM   ?= wg-hub

.PHONY: all hubd hubfind test clean deploy-hub hub-up

all: hubd hubfind

## hubd — control plane, runs on the GCP hub (linux/amd64)
hubd:
	cd hub && GOOS=linux GOARCH=amd64 go build -o ../build/hubd .

## hubfind — discovery agent, runs on each Mac (both architectures)
hubfind:
	cd client/hubfind && GOOS=darwin GOARCH=arm64 go build -o ../../build/hubfind-arm64 .
	cd client/hubfind && GOOS=darwin GOARCH=amd64 go build -o ../../build/hubfind-amd64 .

test:
	cd client/hubfind && go test -v ./...

deploy-hub: hubd
	gcloud compute scp build/hubd $(VM):~ --zone=$(ZONE) --tunnel-through-iap
	gcloud compute ssh $(VM) --zone=$(ZONE) --tunnel-through-iap --command 'set -e; \
		if [ ! -f /etc/systemd/system/hubd.service ]; then \
			echo "no hubd.service on $(VM) — the playbook owns it: run make hub-up" >&2; \
			exit 1; \
		fi; \
		sudo install -m755 ~/hubd /usr/local/bin/hubd; \
		sudo mkdir -p /var/lib/hubd /etc/hubd; sudo chmod 700 /etc/hubd; \
		sudo /usr/local/bin/hubd enroll-key; \
		sudo systemctl restart hubd; sleep 3; systemctl is-active hubd'

clean:
	rm -rf build

## hub-up — pull the repo on the VM and converge it with ansible
hub-up:
	gcloud compute ssh $(VM) --zone=$(ZONE) --tunnel-through-iap --ssh-flag=-A --command 'set -e; \
		if [ -d ~/utils/.git ]; then git -C ~/utils pull --ff-only; \
		else git clone git@github.com:baseproof/utils.git ~/utils; fi; \
		sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ansible >/dev/null; \
		sudo ansible-playbook -i localhost, -c local \
			$$HOME/utils/wg-egress/hub/ansible/playbook.yml'
