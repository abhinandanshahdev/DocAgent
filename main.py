import json
import openai
from fastapi import Depends, FastAPI, HTTPException, Request, Header, UploadFile, File, Body
from typing import Optional, Dict
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import ChatOpenAI
from langchain.schema import StrOutputParser
from langchain.prompts import PromptTemplate
from dotenv import load_dotenv
import os

load_dotenv()  # This loads the environment variables from .env file.

import httpx
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

app = FastAPI()

# CORS middleware setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Function to check if the folder exists and create it if it doesn't
async def ensure_folder_exists(client: httpx.AsyncClient, folder_path: str, headers: dict):
    # Split the folder_path into parts
    folders = folder_path.strip('/').split('/')
    current_path = ""
    for folder in folders:
        # Update the current path at each iteration
        current_path = f"{current_path}/{folder}" if current_path else folder
        check_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{current_path}"
        response = await client.get(check_url, headers=headers)
        
        if response.status_code == 404:  # Folder does not exist
            # Create the folder
            create_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{current_path}:/children"
            body = {
                "name": folder,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "rename"
            }
            create_response = await client.post(create_url, headers=headers, json=body)
            if create_response.status_code not in (200, 201):
                raise HTTPException(status_code=create_response.status_code, detail=create_response.text)
        elif response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        
def get_authorization_header(authorization: str = Header(...)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    return authorization


@app.post("/refined-search")
async def refined_search(query: str, data: Dict = Body(...)):
    try:
        refined_results = refine_search_results(data, query)
        return {"refinedResults": refined_results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upload/{folder_path:path}")
async def upload_file_to_onedrive(
    request: Request,
    folder_path: str, 
    file: UploadFile = File(...),
    authorization: str = None
):
    # If the authorization header is not provided, try to get it from the request headers
    if not authorization:
        authorization = request.headers.get('Authorization')
        print("authorisation header not found non parameter, tried to pick it up from request")
    if not authorization:
        print("despite attempts authorisation header not found")
        raise HTTPException(status_code=401, detail="Authorization header missing")

    folder_path = await determine_folder_path(file.filename) 


    # Check the format of the authorization header
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        print("Invalid authorization header format")
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    access_token = parts[1]
    print("Authorization header is valid")

    auth_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        print("Checking if folder exists...")
        await ensure_folder_exists(client, folder_path, auth_headers)
        print("Folder exists or created.")

        # Once the folder is ensured, prepare to upload the file
        upload_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{folder_path}/{file.filename}:/content"
        upload_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/octet-stream"
        }
        file_content = await file.read()
        print(f"Uploading file {file.filename}...")
        response = await client.put(upload_url, headers=upload_headers, content=file_content)

        if response.status_code not in (200, 201):
            print(f"Failed to upload file: {response.status_code} - {response.text}")
            raise HTTPException(status_code=response.status_code, detail=response.text)

    print("File uploaded successfully.")
    return response.json(), folder_path


# Function to filter out folders and keep only files
def filter_files_only(response):
    # Copy the original response structure, excluding the 'value' key
    filtered_response = {key: response[key] for key in response if key != "value"}

    # Filter the 'value' array to include only items with a 'file' attribute
    filtered_response["value"] = [item for item in response["value"] if "file" in item]
    
    return filtered_response
def refine_search_results(data, query):
    # Parse the JSON data to extract file information
    
    # Creating a prompt for LangChain
    prompt = PromptTemplate.from_template(
        f"""You are a search assistant for a filesystem. Given filename, modified datetime, and user query, please identify a most likely match. Your response must contain "Download URL" and "Name". Stick to context provided. Do not make stuff up. 
        Search Query: "{query}"
        Files:
        {data}

        Most relevant files:"""
    )

    # LangChain processing
    runnable = (
        {"output_text": lambda text: "\n\n".join(text.split("\n\n")[:3])}
        | prompt
        | ChatOpenAI(temperature=0)
        | StrOutputParser()
    )
    return runnable.invoke(data)

@app.post("/determinefolderpath/{filename}")
async def determine_folder_path(filename):
    # Define the prompt with the relevant details
    prompt_text = f"""I want to store the file {filename} in one of the folders from below list of paths. Choose one. Dont give any other answer or explanation. Do not make stuff up.
 [
  "Abhi Shah Montage",
  "Abhin",
  "Abhin/medical",
  "Voice captures"
]"""

    # Create the prompt
    prompt = PromptTemplate.from_template(prompt_text)

    # LangChain processing
    runnable = (
        {"output_text": lambda text: "\n\n".join(text.split("\n\n")[:1])}  # Restrict to the first response for folder name
        | prompt
        | ChatOpenAI(temperature=0.5)  # Adjust temperature as needed
        | StrOutputParser()
    )

    # Invoke the runnable to get the folder path
    folder_path = runnable.invoke(filename)
    print(f"Folder Path suggested:{folder_path}")
    return folder_path


# Function to filter out folders and keep only files
def filter_folders_only(response):
    # Copy the original response structure, excluding the 'value' key
    filtered_response = {key: response[key] for key in response if key != "value"}

    # Filter the 'value' array to include only items with a 'file' attribute
    filtered_response["value"] = [item for item in response["value"] if "folder" in item]

    return filtered_response

@app.get("/search/{query}")
async def search_files(query: str, request: Request):
    authorization_header = request.headers.get("Authorization")
    print(f"Authorization header: {authorization_header}")

    if not authorization_header:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    parts = authorization_header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = parts[1]
    url = f"https://graph.microsoft.com/v1.0/me/drive/root/search(q='{query}')"
    headers = {"Authorization": f"Bearer {token}"}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    data = response.json()
    # Apply the filter to your JSON response
    filtered_response = filter_files_only(data)
    relevant_datapoints_for_llm = extract_drive_item_details(filtered_response)
    llm_result = refine_search_results(relevant_datapoints_for_llm,query)
    print(f"LLM result:{llm_result}")
    # The 'filtered_response' now contains only file items
    #print(json.dumps(filtered_response, indent=4))
    return llm_result

def extract_drive_item_details(json_data):
    # This will store the extracted information as a string
    extracted_data_str = ""

    # Iterate through each item in the 'value' array
    for item in json_data['value']:
        name = item.get('name', 'No Name')
        download_url = item.get('@microsoft.graph.downloadUrl', 'No Download URL')
        last_modified = item.get('lastModifiedDateTime', 'No Last Modified Date Time')
        
        # Add the extracted information to the string
        extracted_data_str += f"Name: {name}\nDownload URL: {download_url}\nLast Modified: {last_modified}\n\n"
        print(extracted_data_str)

    return extracted_data_str.strip()  # Remove the last newline character


async def get_child_folders(client, folder_id, token):
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}/children"
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get(url, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()

async def list_folders_recursive(client, folder_id, token, folder_path, current_depth=0, max_depth=1):
    if current_depth > max_depth:
        return []
    folders_list = []
    data = await get_child_folders(client, folder_id, token)
    for item in data.get("value", []):
        if "folder" in item:
            folder_name = item["name"]
            new_folder_path = f"{folder_path}/{folder_name}".strip("/")
            folders_list.append(new_folder_path)  # Changed here to append only the path
            # Recursively list child folders only if the current depth is less than max_depth
            if current_depth < max_depth:
                child_folders = await list_folders_recursive(
                    client, item["id"], token, new_folder_path, current_depth + 1, max_depth
                )
                folders_list.extend(child_folders)
    return folders_list


@app.get("/listfolders")
async def list_folders(request: Request, authheader: str = None):
    authorization = authheader or request.headers.get("Authorization")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    token = parts[1]
    async with httpx.AsyncClient() as client:
        root_folders = await list_folders_recursive(client, "root", token, "", current_depth=0)
        print(f"Total folders found: {len(root_folders)}")
    return root_folders