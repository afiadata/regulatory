import modal

app = modal.App("regulatory-data")

@app.local_entrypoint()
def main():
    vol = modal.Volume.from_name("regulatory-storage",create_if_missing=True)
    print("Volume 'regulatory-data' created.")

###
if __name__ == "__main__":
    with app.run():
        main()